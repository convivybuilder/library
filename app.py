"""The Library's surface, served to the protocol contract.

`platform/contracts/library-protocol` v1. **The reads are live alongside Orient
and the writes are built but switched off**, which is the whole design of this
stage. The shadow period exists so the Library's answers can be compared against
Orient's on live data before anything moves, and it ends when the reads match for
a full period against real traffic rather than when someone judges it ready.

**Every write route here is unreachable until the cutover, and the connection is
what enforces that** rather than a promise. `store.connect_rw` refuses to open a
writable handle while `LIBRARY_WRITES_ENABLED` is unset, so a route built today
answers 503 and a route added later cannot forget the check. The write surface is
built ahead of the vault migration because the migration cannot land without it.

Deterministic, and no model on any of it (ADR-0019): the same query against the
same corpus state returns the same rows, which is a property a test can hold.

**Every route the contract's §3 table promises is served.** This block used to
name `GET /signals/search` as the one still missing, because the route
enumeration test can only check routes that EXIST and a promised-but-absent one
is what no test catches (reviewer, PR #1). It landed in PR #2 along with the
Reference Room, so what is left here is the warning rather than the gap: a route
added to the contract and not to this file is invisible to the suite, and naming
it here is the only thing that stops it being quietly forgotten.

Agreement with Orient was measured rather than asserted, during the shadow. The
comparison harness asked both services the same question under every credential
and reported where they differed, and it found two: this service listed Courses
by `updated_at` where Orient lists by `created_at`, and returned raw SQLite
timestamps where Orient returns RFC-3339. Both are fixed, and both were invisible
to a suite that only ever compared this service against itself. The harness is
retired now that the shadow period is over.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import math
import os
import hashlib
import functools
import json
import re
import signal
import sqlite3
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send
from fastapi.responses import JSONResponse, PlainTextResponse

import adjacency
import bin.ci_result_fetch as ci_result_fetch
import blobs
import bundles
import call_slips
import credential_binding
import crew_profiles
import crew_registry
import identity
import lease
import librarian_metrics
import library_cards
import machine_identity
import machine_registry
import mtls
import operator_marker
import projects
import profile_sync
import protocol
import reference
import reference_admission
import reference_checkout
import refusals
import runs
import signing_registry
import storage_report
import store
import vectors

# **REFUSED HERE, AT THIS SERVICE'S OWN IMPORT, NOT INSIDE `store.py`.**
# `store.resolve_cold_db_path` is deliberately lazy (its own docstring) so a
# one-time script importing `store` for an unrelated reason is not forced to
# set `LIBRARY_COLD_DB`. This module is the service — the one thing
# archives-cold-tier.md's deploy step 5 means by "the new code refuses to
# start without the variable" — and it crashes at import exactly the way
# `store.DB_PATH = resolve_db()` and `vectors.VECTORS_DB_PATH =
# resolve_vectors_db_path()` already do for their own variables, before
# `serve.sh`'s uvicorn binds a port. The cold FILE's existence is a separate
# question, checked lazily at attach time the same way `LIBRARY_VECTORS_DB`'s
# own file is — see `lifespan`'s startup line, which reports on that without
# refusing to start over it, the same non-fatal shape `vectors.available`
# already gives a missing vectors file.
store.resolve_cold_db_path()

# The page cap. `count` is what this page holds and `total` is the whole set, so a
# caller can tell a short page from a small result — the distinction the contract
# requires and the one a bare list cannot express.
MAX_PAGE = 200

# **`offset` IS BOUNDED AND REFUSED, WHERE `limit` IS BOUNDED AND CLAMPED**, and
# the asymmetry is the point (reviewer, PR #2). Clamping a limit gives the caller
# fewer rows and `truncated` tells them more exist — a partial answer. Clamping an
# offset would return a DIFFERENT page than the one asked for, silently: a wrong
# answer wearing the shape of a right one.
#
# It also has a cost the deterministic routes do not share. `/signals/search`
# couples `offset` into the KNN window, so a large one forces a scan of a
# 44,000-vector index and a proportionally large `IN (...)` clause on every
# request. Paging that deep into a similarity ranking is not a real access
# pattern; narrowing the query is.
MAX_OFFSET = 10_000

# The `signal_id` cap on `GET /signals/acks`, which is the one list-shaped input
# here that a caller sizes rather than the page contract sizing it. Each id costs
# its own `_ack_visible` query, so an uncapped list makes request cost scale with
# a client-supplied count — the only unbounded list input in this file, against a
# convention every other one follows (both reviewers, PR #27).
#
# **REFUSED RATHER THAN TRUNCATED, for the reason `MAX_OFFSET` is refused.**
# Answering the first 200 of 300 ids would report the other 100 as un-acked,
# which is not a partial answer but a wrong one: `ops_reports_build` would render
# an acknowledged alert back into the live section, and the caller has no
# `truncated` flag here to tell it that happened. 200 matches `MAX_PAGE` because
# the ids come FROM a page — a caller that cannot list more than 200 signals at a
# time has no honest way to hold more than 200 ids to ask about.
MAX_ACK_IDS = 200


# **THE COMMIT THIS PROCESS LOADED, RESOLVED ONCE AT IMPORT AND NEVER AGAIN**
# (`issue_01M0K44XH8FP83MVBSXBGRTM96`).
#
# `launchd` serves this repo's working directory directly — `WorkingDirectory` on
# `com.convivy.library`, no separate deploy step — so the tree and the running
# process drift apart on every merge until somebody restarts. On 2026-08-21 that
# gap was 19 hours: PR #45 merged at 16:59 the previous day into a process up
# since 21:50 the day before that, and the service served routes `main` no longer
# had while the checkout said otherwise. Three sessions then asserted
# contradictory things about whether two routes existed, in both directions, over
# several hours — and every one of them had CHECKED. They checked different
# artifacts: the tree, the live `openapi.json`, a test run.
#
# **Resolving this per request would reproduce that bug with more confidence.**
# `git rev-parse HEAD` inside the handler reports what the tree says NOW, which is
# exactly the value that is wrong during the failure this exists to end — it would
# have reported the new commit throughout those 19 hours while serving the old
# routes. The identity being served is the identity that was IMPORTED, so it is
# stamped here, at import, and the handler only reads it.
#
# **It must never raise**, on the same terms as `/health` itself: a liveness probe
# that can fail is worse than no probe. A checkout with no git, a tarball deploy,
# or a `git` that is not on PATH all resolve to `None`, and the field is then
# absent rather than the route being absent.
#
# This is `serve.sh`'s preflight lesson one layer up. That exists because on
# 2026-08-13 the service ran a day under the system python rather than the repo
# `.venv`, so `fastembed` was missing and only one route failed, with a 503 nobody
# was reading. Making the INTERPRETER's identity explicit at start-up was the fix
# there; this is the CODE's identity, and the route's own docstring already
# gestured at it — it said it serves "the protocol versions this build serves"
# while never saying which build.
#
# **platform/decisions/0059: a deploy-area install has no `.git` at all.**
# `bin/install-library` builds the deploy area with `git archive`, which
# exports the tracked tree, not the repository — so `git rev-parse HEAD` in a
# deployed checkout finds nothing to resolve, and this route would silently
# drop the one field the epic tells an operator to read to confirm an install
# took. The installer writes a `DEPLOYED_COMMIT` file into the deploy area
# naming the commit it exported, so this checks that file, in this process's
# own directory, FIRST, and falls back to `git rev-parse HEAD` only for a
# working-checkout run, where there is no such file. Same contract either
# way: never raises, resolved once at import, a malformed value is absent
# rather than wrong.
#
# **THIS FILE HAS NO AUTHENTICATION BEYOND ORDINARY SAME-USER FILE
# PERMISSIONS** (security-reviewer, PR #70 round 1). Any process running as
# this machine's single agent user can write it and arm a restart — that
# crosses no new privilege boundary, since such a process could already
# `kill -TERM` this one for an equivalent disruption. What is worth writing
# down is the other half: a well-formed but bogus sha written here becomes
# the NEXT process's `RUNNING_COMMIT`, so `/health` would report a commit
# that names nothing real, corrupting exactly the drift-visibility this
# feature exists to provide. There is no fix that authenticates the writer —
# a deployed tree has no `.git` to verify a sha against, so this is a
# documented limit, not a gap to close here.
def _read_deployed_commit(path: str) -> str | None:
    """The `commit:` line of a DEPLOYED_COMMIT file, or None.

    None for a file that is missing, unreadable, undecodable as UTF-8, has no
    `commit:` line, or whose value is not 40 lowercase hex digits — the same
    shape check `_git_rev_parse_head` applies to `git`'s output, and for the
    same reason: a value that could be wrong, not attacker-controlled
    (nothing here is reachable from a request), so this is about
    correctness, not hostility.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, ValueError):
        # `ValueError` catches `UnicodeDecodeError`: invalid UTF-8 bytes in
        # the file raise that, not `OSError`, and this ran uncaught until a
        # reviewer caught it (reviewer, PR #70 round 1) — it would have
        # crashed `RUNNING_COMMIT = _running_commit()` at import, and
        # post-boot, 500'd every `/health` call and silently killed the
        # watcher task the same way any other unhandled exception in it does.
        return None
    sha = None
    for line in text.splitlines():
        if line.startswith("commit:"):
            sha = line[len("commit:"):].strip()
            break
    if sha is None or len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        return None
    return sha


def _git_rev_parse_head(dir_: str) -> str | None:
    """`git rev-parse HEAD` in `dir_`, shape-checked, never raising.

    The working-checkout fallback `_running_commit` and `_staged_commit` both
    use when this process's own directory has no DEPLOYED_COMMIT file.
    """
    import subprocess
    try:
        out = subprocess.run(
            ["git", "-C", dir_, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5)
    except Exception:  # noqa: BLE001 — no failure here may reach the probe
        return None
    if out.returncode != 0:
        return None
    sha = out.stdout.strip()
    # **Checked against the shape rather than trusted on the exit code**
    # (reviewer, convivy/library#59). A git wrapper, alias or hook that exits 0
    # while printing something else would otherwise pass straight through into
    # the boot log and the probe. Nothing here is attacker-controlled — the argv
    # is fixed and no request reaches it — so this is about a value that is
    # wrong rather than one that is hostile, which is the failure this whole
    # route exists to end.
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        return None
    return sha


def _running_commit(dir_: str | None = None) -> str | None:
    """The commit this process loaded: this directory's DEPLOYED_COMMIT file
    if it has one (a deploy-area install), else `git rev-parse HEAD` (a
    working checkout), else None.

    `dir_` defaults to this file's own directory. It exists as a parameter so
    a test can exercise the resolution logic against a fixture directory
    without reimporting this module — every call this module itself makes
    uses the default, and RUNNING_COMMIT below is the only one that runs at
    import time, exactly once, never per request; see the block comment
    above for why that distinction is the whole point.
    """
    if dir_ is None:
        dir_ = os.path.dirname(os.path.abspath(__file__))
    sha = _read_deployed_commit(os.path.join(dir_, "DEPLOYED_COMMIT"))
    if sha is not None:
        return sha
    return _git_rev_parse_head(dir_)


RUNNING_COMMIT = _running_commit()


def _staged_commit(dir_: str | None = None) -> str | None:
    """The commit currently sitting in this process's own directory's
    DEPLOYED_COMMIT file — read FRESH on every call, unlike RUNNING_COMMIT.

    **This is what makes deploy/restart drift readable instead of
    discovered.** `bin/install-library` can replace the deploy area's
    DEPLOYED_COMMIT file at any moment, independent of this process, which
    only picks up the new code on its next restart. RUNNING_COMMIT is frozen
    at import so it always answers "what is THIS PROCESS serving"; this
    answers "what is on disk right now", live, so `/health` can report both
    and the gap between them — the exact gap that made the 2026-08-20
    incident invisible for 19 hours — is visible without anyone having to go
    looking.

    None for a working checkout, which has no DEPLOYED_COMMIT file: there is
    no separate staged state to report there, and this deliberately does not
    fall back to a live `git rev-parse HEAD` — that would reintroduce, for
    "staged", the exact per-request git resolution the module comment above
    rules out for "running", for a value nothing here has asked to track.
    """
    if dir_ is None:
        dir_ = os.path.dirname(os.path.abspath(__file__))
    return _read_deployed_commit(os.path.join(dir_, "DEPLOYED_COMMIT"))


# --- platform/decisions/0059: quiesce-and-restart ---------------------------
#
# Once installed from a deploy area, this process goes stale the moment a
# newer commit is staged — `_staged_commit()` says so before this process
# ever restarts on its own. Restarting is launchd's job: `KeepAlive` on
# `com.convivy.library` brings the process back the instant it exits. All
# this needs to do is exit at a moment that will not drop a request.
#
# **A healthy idle process and a wedged one both read zero in-flight
# requests, so a bare in-flight count is not evidence of quiet.** What makes
# a zero trustworthy is refusing new work FIRST: once nothing more can be
# admitted, the count can only fall, and a zero observed after that is a real
# drain rather than a lull between two arrivals that happened to straddle the
# read. Refuse-then-confirm is the only order that makes the zero mean
# anything; confirm-then-refuse would let a request slip in during the gap.
#
# **A stuck request is bounded by `_DRAIN_DEADLINE_SECONDS`, not held open
# forever — a correction to the original design, not a mistake in
# implementing it** (the operator, PR #70 round 1). Quiescing refuses every
# non-health route the instant it arms, so an unbounded wait here is not
# merely "the restart waits a bit longer": it is every agent on this machine
# locked out of the credentialed Library for as long as ANY one in-flight
# request takes, with no ceiling — and nothing here distinguishes a
# genuinely slow request, where waiting protects a write from being torn,
# from a permanently wedged one that will never finish and gains nothing
# from the wait. Past the deadline, the process force-exits and drops
# whatever is still outstanding: a dropped request is recoverable
# (`KeepAlive` already treats process exit as routine), an unbounded
# fleet-wide lockout is not. See `_DRAIN_DEADLINE_SECONDS` below for how that
# number was derived.
_QUIESCING = False
_IN_FLIGHT = 0

# Poll intervals, not the request path: a DEPLOYED_COMMIT read every few
# seconds while healthy, an in-flight check every fraction of a second once
# draining starts.
_RESTART_POLL_SECONDS = 5.0
_DRAIN_POLL_SECONDS = 0.2

# **THE DRAIN DEADLINE, MEASURED FROM THE CODE RATHER THAN PICKED BY FEEL.**
# The longest legitimate request this service serves is a single-item write
# that embeds under SQLite's write lock: `_embed_after_commit`'s own
# docstring cites Orient's production measurement of eight concurrent posts,
# six of them waiting out `connect_rw`'s 5,000 ms `busy_timeout` and returning
# 500, the slowest at **32.2 seconds**. Nothing else on the request path is
# slower —
# `/reference/search` is deterministic FTS5 with no model at all (ADR-0019),
# `/signals/search` embeds one query, not a batch, and `vectors.embed_missing`'s
# multi-second sweep over hundreds of rows runs in `replay_stranded.py`, a
# separate process this watcher's `_IN_FLIGHT` count never sees.
#
# **A SECOND CEILING NOW BOUNDS THIS FROM ABOVE, NOT JUST THE MEASURED
# WORST CASE FROM BELOW.** `serve_dual._GRACEFUL_SHUTDOWN_SECONDS` wires
# uvicorn's own graceful-shutdown timeout to this same constant, for a raw
# SIGTERM — `bin/install-library`'s own `bootout` on a reload, or any other
# source — rather than the self-initiated quiesce this docstring covers.
# `launchd/com.convivy.library.plist`'s `ExitTimeOut` is what actually races
# that shutdown: launchd SIGKILLs the process once `ExitTimeOut` elapses
# since the SIGTERM, whatever this constant says. The plist's own comment
# records the clamp (issue_01M3N7NB9218HXKQDPD3NF1BJH): `launchctl print`
# reports `exit timeout = 60` on this machine regardless of the larger value
# the plist asks for, a gui-domain LaunchAgent limit `man launchd.plist`
# does not document. A value here at or above that 60 s gives launchd's
# SIGKILL nothing to wait for — it lands at or before the graceful drain
# would have finished on its own, dropping the same in-flight write this
# constant exists to protect.
#
# 50 s clears both ceilings: 10 s under launchd's 60 s clamp, and a 1.55x
# margin over the 32.2 s worst case above — down from 60 s's 1.86x, because
# the ceiling moved, not because the worst case was re-measured. **The
# margin is stated as a ratio because an earlier version of this comment
# said "roughly double, rounded up" for 60 s and 60 was neither** — twice
# 32.2 is 64.4, so 60 rounded DOWN from the doubled figure (reviewer,
# convivy/library#70). The number changed since for an unrelated reason (the
# launchd ceiling above); the arithmetic convention from that finding is
# kept so the next person can re-derive this value rather than trust it.
_DRAIN_DEADLINE_SECONDS = 50.0


async def _restart_watcher() -> None:
    """Poll for a staged commit that differs from RUNNING_COMMIT; once found,
    stop accepting new work, wait for a genuine drain, then exit so
    `KeepAlive` restarts this process on the new code.

    **Never arms on an unresolved commit on either side.** A None from
    `_staged_commit()` or from `RUNNING_COMMIT` means "cannot tell", and
    "cannot tell" must not authorize a restart any more than a hung counter
    should — the same principle the drain loop below applies to in-flight
    work, applied here to the comparison that starts it.

    **The drain waits for a genuine zero, up to `_DRAIN_DEADLINE_SECONDS` —
    past that it force-exits instead of sending another SIGTERM.** SIGTERM
    would hand the same wait to uvicorn's own graceful shutdown, which blocks
    on the same stuck ASGI task for the same reason. `serve_dual` bounds that
    shutdown with this same constant (`timeout_graceful_shutdown` on both its
    configs), but its clock starts at the signal: a second full
    `_DRAIN_DEADLINE_SECONDS`, where this drain has already spent its own.
    `os._exit` is what actually ends the fleet-wide lockout now, at the cost
    of the one request still outstanding.

    Runs as a background task started in `lifespan` and cancelled on
    shutdown; never touches the request path itself, which is the refusing
    middleware's job.
    """
    global _QUIESCING
    while not _QUIESCING:
        await asyncio.sleep(_RESTART_POLL_SECONDS)
        staged = _staged_commit()
        if staged and RUNNING_COMMIT and staged != RUNNING_COMMIT:
            print(f"library: staged commit {staged} differs from running "
                  f"{RUNNING_COMMIT} — quiescing for restart", file=sys.stderr)
            _QUIESCING = True

    deadline = time.monotonic() + _DRAIN_DEADLINE_SECONDS
    while _IN_FLIGHT > 0 and time.monotonic() < deadline:
        await asyncio.sleep(_DRAIN_POLL_SECONDS)

    if _IN_FLIGHT > 0:
        # LOUD, and on purpose: this is the signal that something is
        # genuinely wedged, not the routine restart path, and it is the only
        # record of the dropped request(s) — the process is about to exit
        # without running any further cleanup.
        print(f"library: DRAIN DEADLINE HIT after {_DRAIN_DEADLINE_SECONDS:.0f}s "
              f"WITH {_IN_FLIGHT} REQUEST(S) STILL IN FLIGHT — force-exiting "
              f"and dropping them; KeepAlive restarts immediately and already "
              f"treats a dropped request as recoverable", file=sys.stderr)
        sys.stderr.flush()
        # `os._exit`, not SIGTERM: SIGTERM re-enters uvicorn's own graceful
        # shutdown, which waits on the identical stuck task for the identical
        # reason. `serve_dual` bounds that shutdown now, but with a fresh
        # full `_DRAIN_DEADLINE_SECONDS` starting at the signal; this drain
        # has already spent its own, so handing off only relocates the wait.
        # `os._exit` terminates immediately, skipping cleanup, which is
        # exactly what dropping the stuck request means.
        os._exit(1)
        return  # unreachable outside a test that stubs `os._exit`

    print("library: quiesced with zero in-flight requests — restarting",
          file=sys.stderr)
    # SIGTERM, not a bare process exit: uvicorn already handles SIGTERM as a
    # graceful shutdown signal (it is what lets `serve.sh`'s `exec` forward
    # launchd's own SIGTERM), so self-sending it reuses that shutdown path
    # rather than tearing the event loop down out from under it. Safe to wait
    # on here because the drain above already confirmed zero in flight, so
    # there is nothing left for uvicorn's own graceful wait to block on.
    os.kill(os.getpid(), signal.SIGTERM)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Say what this process can authenticate and which database it will read.

    Both are silent failures otherwise: a token file the Library's user cannot
    read refuses every request with a 401 and no other symptom, and a wrong
    database path is a wrong answer rather than an error. One line at boot beats
    diagnosing either from a client's refusal.

    **The database line carries the path, the variable it came from, and whether
    writes are on, because it is a step in the cutover rather than a nicety.**
    Step 6 sets `LIBRARY_DB` and `LIBRARY_WRITES_ENABLED` together and tells the
    operator to confirm the new path at boot before continuing; the path alone
    cannot distinguish "this is the file I exported" from "this is the file this
    process fell back to", and those are the two outcomes the step is checking
    between.

    A lifespan handler rather than `@app.on_event("startup")`, which FastAPI
    deprecates — a new repo has no reason to ship a deprecation warning on its
    first commit.
    """
    print(f"library: serving protocol {protocol.CURRENT} "
          f"(supports {list(protocol.SUPPORTED)})", file=sys.stderr)
    # "unresolved" rather than naming a cause: git can be absent from PATH, the
    # directory can be outside any checkout, git can hang past the timeout, or it
    # can exit 0 with output that is not a hash. The line should not assert which
    # (reviewer, convivy/library#59).
    print(f"library: code {RUNNING_COMMIT or 'unresolved'}", file=sys.stderr)
    print(f"library: database {store.DB_PATH} (from {store.DB_SOURCE}), "
          f"writes "
          f"{'ENABLED' if store.WRITES_ENABLED else 'disabled — read-only shadow'}",
          file=sys.stderr)
    print(f"library: {identity.describe_credentials()}", file=sys.stderr)
    # `_semantic_search_readiness`, not a bare `vectors.available()`: this is
    # also the readiness check's first run, seeding `_readiness_cache` before
    # `_readiness_refresh_loop` starts and before any request can arrive, so
    # `health()` never finds the cache empty on a real deploy. One call does
    # both jobs rather than checking readiness twice at boot.
    ok, why = _semantic_search_readiness()
    print(f"library: semantic search {'ready' if ok else 'UNAVAILABLE — ' + why}",
          file=sys.stderr)
    # `LIBRARY_COLD_DB` itself is REQUIRED (refused above, at this module's own
    # import). The FILE existing is checked here instead, the same non-fatal
    # shape as the vectors line just above: a missing file is reported, not a
    # reason to refuse to start, because every read path that needs it already
    # answers its own caller a 503 through `attach_cold_or_reason`.
    cold_ok, cold_why = store.cold_db_available()
    print(f"library: archive tier "
          f"{'ready' if cold_ok else 'UNAVAILABLE — ' + cold_why}", file=sys.stderr)
    print(f"library: merge admission "
          f"{'ENFORCED' if store.MERGE_ADMISSION_ENFORCED else 'STUBBED (always approves)'}",
          file=sys.stderr)
    watcher = asyncio.create_task(_restart_watcher())
    readiness_refresher = asyncio.create_task(_readiness_refresh_loop())
    try:
        yield
    finally:
        # Cancelled rather than left to be garbage-collected: a task still
        # pending when the event loop closes prints "Task was destroyed but
        # it is pending" on every process exit, which is noise this exists to
        # avoid, not a functional requirement — the process is exiting either
        # way.
        watcher.cancel()
        readiness_refresher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
        try:
            await readiness_refresher
        except asyncio.CancelledError:
            pass


app = FastAPI(title="The Library", version=str(protocol.CURRENT),
              lifespan=lifespan)


@app.exception_handler(identity.CredentialStoreError)
async def unresolvable_credentials(_: Request, exc: identity.CredentialStoreError):
    """Backstop for the store going bad between the middleware's read and a
    route's own `resolve()` — the same refusal, from the same cause."""
    print(f"library: REFUSED A REQUEST — {exc}", file=sys.stderr)
    return refusals.refuse(
        503, "the Library cannot resolve its credential store",
        hint="the operator must fix the client credential file; the "
             "server log names the entry.")


@app.exception_handler(store.MisconfiguredDatabase)
async def misconfigured_database(_: Request, exc: store.MisconfiguredDatabase):
    """A database this process can no longer identify, refused in the contract's
    shape rather than as a bare 500.

    **This is reachable only through `connect_rw`'s post-boot re-check**, since
    the same refusal at import stops the process before it binds a port. The
    write is already blocked either way — the connection is never returned — so
    what this adds is the refusal a caller can read: a 503 naming what failed,
    which is what every other refusal on these routes does and what an incident
    at the cutover would otherwise be missing.

    **A handler rather than a catch at each site**, matching the version
    middleware's reasoning: twenty call sites already wrap `WritesDisabled`, and
    a route added later must not be able to forget this one.

    **THE DETAIL GOES TO THE LOG AND NOT TO THE CALLER** (security-reviewer,
    convivy/library#32). The refusal's message names the absolute path of the
    file it refused, and `hint=str(exc)` would have handed that to any client
    holding an ordinary write capability — the service account's home directory
    and on-disk layout, disclosed to a caller with no reason to see it. The
    convention it copied is safe for `WritesDisabled`, whose message is a fixed
    string; this is the first refusal here whose text carries server-side
    material.

    And the log line is what makes that safe rather than merely quiet: the boot
    line prints the database once and does not fire again, so a swap after boot
    would otherwise leave the only record of what was refused in a response
    nobody keeps.
    """
    print(f"library: REFUSED A WRITE — {exc}", file=sys.stderr)
    return refusals.refuse(
        503, "the Library cannot identify its own database",
        hint="the configured database no longer identifies as this service's "
             "own file, so the write was refused rather than risking a second "
             "writer. The detail is in the service log.")


@app.exception_handler(store.ColdTierUnavailable)
async def cold_tier_unavailable(_: Request, exc: store.ColdTierUnavailable):
    """`insert_signal`'s dedup fallback could not confirm a `cold_dedup_keys`
    hit's row, refused in the contract's shape rather than as a bare 500.

    **Covers `POST /signals` only.** `_record_review_signal` catches
    `store.ColdTierUnavailable` itself and swallows it, matching
    `store.WritesDisabled` there, because its own best-effort contract says a
    failure here must not undo the review-record route's already-committed
    answer — this handler is never reached from that path.

    **Same fixed wording every read path's `attach_cold_or_reason` already
    answers with** (`exc.reason`, set by `store.cold_unavailable_reason`,
    which has already logged the full exception) — no second vocabulary for
    "the cold tier could not be opened".
    """
    return refusals.refuse(503, f"the archive tier is unavailable: {exc.reason}")


@app.exception_handler(reference.ReferenceIndexUnavailable)
async def reference_index_unavailable(_: Request, exc: reference.ReferenceIndexUnavailable):
    """The knowledge index could not be opened, refused in the contract's shape
    rather than as a bare 500.

    **A handler rather than a check at each of the Reference Room's six routes**,
    matching `misconfigured_database` just above for the same reason: a seventh
    route calling `reference.connect()` is covered without knowing anything
    exists, instead of inheriting a 500 the way the six existing routes did —
    twelve occurrences in `serve.log` on 2026-09-03, all through
    `list_reference_documents` (`issue_01M1N7J265ST2V6VZ2C9TA4BN7`).

    **THE DETAIL GOES TO THE LOG AND NOT TO THE CALLER**, matching
    `misconfigured_database`'s own reasoning (security-reviewer, convivy/library#32).
    `str(exc)` names the index's absolute filesystem path — and, through it, the
    service's home-directory layout and OS username — to any caller holding
    `reference.read`, which is a capability this file grants far more broadly
    than write access. Previously that string reached only a server-side
    traceback; the fixed hint below names the condition and where to look
    without naming the path.
    """
    print(f"library: reference index unavailable — {exc}", file=sys.stderr)
    return refusals.refuse(
        503, "the knowledge index is unavailable",
        hint="the knowledge index could not be opened; check LIBRARY_KB_INDEX. "
             "The detail is in the service log.")


@app.exception_handler(sqlite3.OperationalError)
async def database_busy(request: Request, exc: sqlite3.OperationalError):
    """A statement that waited out SQLite's busy timeout, answered 503 with
    `Retry-After` instead of a 500.

    **The service is working; another connection held the lock.** A 500 tells a
    caller something broke and leaves it to guess whether to retry. `serve.log`
    held eight `database is locked` tracebacks when this was written: four
    through `_hold_for_write` on the link-PR route and four through the
    `get_item` and `list_courses` reads, so both halves of the surface reach it.

    **A handler rather than a catch at each route**, the same reasoning as the
    two handlers above: every route opening a connection is covered, including
    one added later.

    **Only `SQLITE_BUSY` becomes a 503, and every other `OperationalError`
    re-raises into the 500 it was before.** A missing table or a read-only file
    is a fault, and relabelling it as contention would tell the caller to retry
    something that will never succeed. The mask keeps the extended codes
    (`SQLITE_BUSY_RECOVERY`, `SQLITE_BUSY_SNAPSHOT`), which are the same
    condition.

    **The body does not say whether anything was written.** A route that
    commits one step and hits the lock on a later one has written something,
    and a caller told "nothing was written" would retry into a duplicate.
    `Retry-After` is one busy timeout, rounded up to whole seconds, because a
    retry sooner than that queues behind the same writer.
    """
    if exc.sqlite_errorcode & 0xFF != sqlite3.SQLITE_BUSY:
        raise exc
    # **The matched route's template, never the raw path** (security-reviewer,
    # convivy/library#112). A path parameter is percent-decoded caller bytes,
    # so `GET /items/%0d%0a...` would write a forged line into `serve.log`; the
    # template carries none of them, and groups the busy lines by route.
    print(f"library: database busy — {request.method} "
          f"{request.scope['route'].path}", file=sys.stderr)
    retry_after = max(1, math.ceil(store.BUSY_TIMEOUT_MS / 1000))
    response = refusals.refuse(
        503, "the Library's database is busy",
        hint=f"another connection held the database lock past the "
             f"{store.BUSY_TIMEOUT_MS} ms busy timeout. Retry after "
             f"{retry_after} s.")
    response.headers["Retry-After"] = str(retry_after)
    return response


class RequestBodySizeLimit:
    """Refuse a request body larger than `store.MAX_REQUEST_BYTES` before anything
    reads it.

    **A RAW ASGI MIDDLEWARE RATHER THAN AN `@app.middleware("http")` ONE, and the
    difference is the whole point.** The decorator form is `BaseHTTPMiddleware`,
    which hands the route a `Request` whose body is read on demand — by then
    `await request.json()` has already buffered and parsed the entire payload on
    the event loop, which is the cost this exists to refuse. Sitting on the raw
    `receive` channel is what lets it count bytes as they arrive and stop.

    **Two paths, because a caller controls whether it declares a length.**
    `Content-Length` is the fast one and refuses before a single body byte is
    read. A chunked or undeclared body has no length to check, so the bytes are
    counted as they arrive and the refusal fires on the chunk that crosses the
    line. A cap that trusted the header alone would be bypassed by omitting it,
    which is the shape a caller sending a hostile body would reach for first.

    **`store.MAX_REQUEST_BYTES` is `MAX_BLOB_BYTES` times the measured worst-case
    JSON escape expansion**, so this cannot refuse a body a route would have
    accepted — in any script, under either encoder setting, control characters
    included. The guarantee is absolute rather than bounded, and the constant's
    comment says what two earlier attempts at a smaller multiplier cost.

    **It does not replace the per-field caps and must not be read as doing so.**
    Those bound a column or a disk write; this bounds what the process will hold
    in memory before any of them get a chance to run. `content` is still checked
    against `MAX_BLOB_BYTES` after parsing, and that check is what names the real
    limit to a caller who is merely over it rather than pathological.
    """

    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # **THE FIRST `content-length` IS THE ONLY ONE READ, and that is safe
        # because the server settles duplicates before ASGI** (reviewer and
        # security-reviewer, PR #66, the second having read the installed h11).
        # h11 refuses two conflicting `Content-Length` headers with a protocol
        # error and collapses identical ones to a single value, so a scope
        # carrying two different lengths does not occur here. Named rather than
        # left implicit, since the counter below is what actually holds if a
        # laxer server ever puts one through.
        for key, value in scope.get("headers", ()):
            if key != b"content-length":
                continue
            try:
                declared = int(value)
            except ValueError:
                await self._refuse(scope, receive, send, refusals.refuse(
                    400, "Content-Length is not an integer",
                    hint="send a decimal byte count, or omit the header and "
                         "send a chunked body"))
                return
            # **A NEGATIVE LENGTH IS REFUSED RATHER THAN IGNORED.** `int(b"-1")`
            # parses, and a negative declared length passes any `> max` test
            # while describing a body that cannot exist. Refusing it here means
            # the only lengths that reach the counter are ones that mean
            # something.
            if declared < 0:
                await self._refuse(scope, receive, send, refusals.refuse(
                    400, "Content-Length is negative", declared=declared))
                return
            if declared > self.max_bytes:
                await self._refuse(scope, receive, send,
                                   self._too_large("declared", declared))
                return
            break

        # **THE COUNTER RUNS EVEN WHEN A LENGTH WAS DECLARED AND PASSED**, because
        # the header is the caller's claim about the body rather than the body. A
        # request declaring 10 bytes and sending 40 MB is refused here on the
        # chunk that crosses, not trusted on its own description of itself.
        read = 0

        async def counting_receive():
            nonlocal read
            message = await receive()
            if message["type"] == "http.request":
                read += len(message.get("body", b""))
                if read > self.max_bytes:
                    raise _BodyTooLarge(read)
            return message

        try:
            await self.app(scope, counting_receive, send)
        except _BodyTooLarge as exc:
            # **THE RESPONSE MAY ALREADY HAVE STARTED**, since this fires from
            # inside the application while it reads. Sending a second
            # `http.response.start` then fails, so the refusal is attempted and
            # its failure swallowed: the connection breaks either way and the
            # caller learns the same thing. What matters is that the read
            # stopped.
            #
            # **BOTH EXCEPTION TYPES, BECAUSE THE SERVER DECIDES WHICH ONE**
            # (reviewer, PR #66). uvicorn's h11 implementation raises
            # `RuntimeError` for a message sent after the response started;
            # httpx's `ASGITransport`, which this suite's `TestClient` runs on,
            # asserts instead and raises `AssertionError`. Catching only the
            # first ties a correctness property to one deployment's server and
            # would fail under the very transport the tests use.
            #
            # Unreachable today: every route reads its whole body with one
            # `await request.json()` before producing any response, so nothing
            # starts a response while the counter is running. It becomes
            # reachable the day a route streams a response while still reading —
            # which is why this is written for that day rather than left to be
            # discovered on it.
            try:
                await self._refuse(scope, receive, send,
                                   self._too_large("received", exc.read))
            except (RuntimeError, AssertionError):
                pass

    def _too_large(self, field: str, size: int) -> JSONResponse:
        """**THE FIELD NAMES WHICH CHECK FIRED, because the two are not the same
        fact and reporting both as `received` is a refusal that lies.** On the
        fast path nothing has been received at all — the header was read and the
        body refused before a byte arrived — so calling that number `received`
        tells a caller the Library buffered 28 MB it never touched, which is the
        opposite of what this middleware exists to do. `declared` is the
        caller's own claim; `received` is what was actually counted."""
        return refusals.refuse(
            413, f"request body is larger than {self.max_bytes} bytes",
            hint="this bounds the whole request, not one field; a large "
                 "`content` belongs in fewer, smaller signals",
            limit=self.max_bytes, **{field: size})

    async def _refuse(self, scope, receive, send, response: JSONResponse) -> None:
        """Through the same `refusals` shape every other refusal uses.

        A `JSONResponse` is itself an ASGI app, so the contract's §8 body reaches
        the caller from out here without this middleware growing its own private
        way of spelling an error — which is how the one shape a client parses
        stops being one shape.
        """
        await response(scope, receive, send)


class _BodyTooLarge(Exception):
    def __init__(self, read: int):
        super().__init__(read)
        self.read = read


class RequestDeadline:
    """Bound a request's total wall-clock time to `store.REQUEST_DEADLINE_
    SECONDS`, past which the connection is refused rather than held open.

    platform/decisions/0066, stage B. **This is the revocation bound**, not a
    generic timeout added for its own sake: the mutual-TLS registry
    (`machine_identity`) is read fresh on every request, so a certificate
    revoked mid-connection is refused starting on the very next request — but
    "worst case, a revoked certificate keeps working for the length of one
    already-running request" is only a bound if that one request itself has
    a ceiling. uvicorn supplies none. Before this middleware existed, a
    revoked client that had already opened a long-running or slow-drip
    request faced no limit at all, for as long as it could hold the
    connection — which is why the decision record makes this a build-order
    requirement for stage B rather than a preference: shipping the TLS
    listener and registry revocation without it would put a revocation
    mechanism into service whose advertised bound does not hold.

    **Scoped to the mutual-TLS listener only, the same way `machine_identity.
    MachineIdentityGate` is.** The plain port at 127.0.0.1:8100 has no
    certificate to revoke and this migration's hard constraint is that it
    serves exactly as it does today — so this checks the same
    `mtls.EXTENSION_KEY` scope extension the identity gate does, and a
    request that arrived over the plain listener never carries it and passes
    through untouched, unconditionally, on the first line.

    `asyncio.wait_for` around the whole inner ASGI call, not a
    `BaseHTTPMiddleware` dispatcher: this needs to bound the request
    regardless of whether the timeout lands before or after the handler has
    started writing a response, and cancelling the awaited coroutine is what
    actually stops the work rather than merely stops waiting for it.
    """

    def __init__(self, app: ASGIApp, seconds: float):
        self.app = app
        self.seconds = seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or mtls.EXTENSION_KEY not in scope.get(
                "extensions", {}):
            await self.app(scope, receive, send)
            return
        try:
            await asyncio.wait_for(self.app(scope, receive, send),
                                   timeout=self.seconds)
        except asyncio.TimeoutError:
            # **THE RESPONSE MAY ALREADY HAVE STARTED**, the same reason
            # `RequestBodySizeLimit._refuse` catches both exception types
            # below: uvicorn's h11 implementation raises `RuntimeError` for a
            # message sent after the response started, and httpx's
            # `ASGITransport` — what this suite's `TestClient` runs on —
            # raises `AssertionError` for the same thing instead. The
            # connection is gone either way once the deadline fires; what
            # matters is that the caller is not left waiting past it.
            response = refusals.refuse(
                504, f"request exceeded the {self.seconds:g}s deadline",
                hint="this bounds one request's total time, not one route's "
                     "typical time; retry as a new request rather than a "
                     "longer one")
            try:
                await response(scope, receive, send)
            except (RuntimeError, AssertionError):
                pass


@app.middleware("http")
async def negotiate_version(request: Request, call_next):
    """Every request declares its protocol version, or is refused.

    **A MIDDLEWARE RATHER THAN A DEPENDENCY ON EACH ROUTE**, because a route added
    later must not be able to forget it. The check that a new endpoint silently
    skips is the check that stops being true.

    `/health` is exempt: a liveness probe is not a protocol client, and requiring
    the header there would mean a monitoring tool has to know the protocol to ask
    whether the process is up.
    """
    if request.url.path == "/health":
        return await call_next(request)
    version, err = protocol.negotiate(request.headers.get(protocol.HEADER))
    if err:
        return JSONResponse(err, status_code=400)
    request.state.protocol_version = version
    response = await call_next(request)
    # Answer with the version served, so a client can assert on it rather than
    # assuming its request header was honoured.
    response.headers[protocol.HEADER] = str(version)
    return response


@app.middleware("http")
async def refuse_on_unresolvable_credentials(request: Request, call_next):
    """Serve no request while the Library's own client credential store cannot
    be resolved (unreadable, not a JSON object, or an entry naming an unknown
    capability in `grants`, or a human-reserved capability in either list).
    An entry naming an unknown capability in `withholds` does not trigger
    this: it resolves and is reported instead (`identity._entry_capabilities`,
    `dead_withholds`).

    **A MIDDLEWARE, SO NO ROUTE CAN FORGET IT, AND `/health` IS NOT EXEMPT.**
    The other middlewares exempt it because a probe is not a protocol client;
    here a 200 from `/health` over a store the Library cannot resolve is the
    lie, since a deploy that gates on it would call a broken credential file
    healthy. The refusal names the entry and capability in the LOG and not in
    the body: the message can carry a path, and the caller has no use for it.
    """
    err = identity.credential_store_error()
    if err is not None:
        print(f"library: REFUSED A REQUEST — {err}", file=sys.stderr)
        return refusals.refuse(
            503, "the Library cannot resolve its credential store",
            hint="the operator must fix the client credential file; the "
                 "server log names the entry.")
    return await call_next(request)


@app.middleware("http")
async def refuse_during_quiesce(request: Request, call_next):
    """Refuse new work once a restart is quiescing, and count everything else
    in flight so `_restart_watcher` knows when it is safe to exit.

    `/health` is exempt from both, same reasoning as `negotiate_version`
    above: it must stay answerable through a quiesce so the refusing state is
    observable there, and it is a probe, not the work the drain is waiting on.

    **Registered here, wrapping `negotiate_version` but still inside
    `RequestBodySizeLimit`** (see the comment on that call below): a request
    refused here is refused before it pays for protocol negotiation, and the
    body cap still runs outermost of everything, unchanged.

    **Refuse BEFORE counting, not after.** Admitting a request and refusing
    it inside the handler would let it show up in `_IN_FLIGHT` briefly before
    the refusal completed — exactly the false "in-flight, then not" reading
    the quiesce logic exists to rule out. A request refused here was never
    admitted at all.

    **What the count actually guarantees is narrower than "admitted and not
    yet finished"** (reviewer, PR #70 round 1): the decrement runs in
    `call_next()`'s `finally`, and `call_next` returns once the response's
    headers are ready, not once its body has finished streaming to the
    client and not once a `Response.background` task has run. No route here
    uses `StreamingResponse`, a websocket, or `BackgroundTasks` today, and
    uvicorn's own graceful shutdown waits out a connection's in-flight ASGI
    task regardless of what this counter reads — so nothing is dropped in
    practice. But the day a route attaches one of those, the watcher can read
    zero while that work is still running, and only uvicorn's own shutdown
    grace — not this module's drain — would be standing between that work
    and a restart.
    """
    global _IN_FLIGHT
    if request.url.path == "/health":
        return await call_next(request)
    if _QUIESCING:
        return JSONResponse(
            {"error": "restart pending — quiescing, refusing new work"},
            status_code=503)
    _IN_FLIGHT += 1
    try:
        return await call_next(request)
    finally:
        _IN_FLIGHT -= 1


# **ALL THREE SCOPED TO THE MUTUAL-TLS LISTENER, AND ALL THREE INSIDE THE
# BODY CAP.** `machine_identity.MachineIdentityGate`, `credential_binding.
# CredentialBindingGate` and `RequestDeadline` each check `mtls.
# EXTENSION_KEY` themselves and pass a plain-port request straight through,
# so registering them here changes nothing about 127.0.0.1:8100.
#
# Registration order below is gate first, binding gate second, deadline
# third. `add_middleware` makes the LAST-registered middleware the
# OUTERMOST (see the body cap's own registration comment below), so from
# outermost to innermost this reads: deadline, machine-identity gate,
# credential-binding gate, route.
#
# **The machine-identity gate has to run before the binding gate can mean
# anything**: `credential_binding.CredentialBindingGate` reads `scope
# ["state"]["machine_identity"]`, which only `machine_identity.
# MachineIdentityGate` ever sets, and only once it has admitted the
# request. Registering the identity gate second — after the binding gate,
# which therefore ends up the INNER of the two — is what makes the
# identity gate's check, including setting that scope state, run before
# the binding gate's own check does.
#
# **The deadline wraps both**, same reasoning as before it had two inner
# gates rather than one: a hung registry read in either gate should be
# bounded by the same deadline as a hung route, not left unbounded because
# it happens before the route itself starts.
app.add_middleware(credential_binding.CredentialBindingGate)
app.add_middleware(machine_identity.MachineIdentityGate)
app.add_middleware(RequestDeadline, seconds=store.REQUEST_DEADLINE_SECONDS)

# **THE OUTERMOST MIDDLEWARE, AND IT MUST STAY THAT WAY.** Starlette applies
# `add_middleware` in reverse, so the LAST one registered wraps every other. A
# middleware added below this line would sit outside the body cap and read an
# unbounded body itself, which is the one thing this exists to prevent — so a
# new middleware goes ABOVE, and this call stays last.
#
# `negotiate_version` and `refuse_during_quiesce` are both deliberately inside
# it: refusing a 40 MB body costs nothing whether or not its protocol header
# was right or a restart is quiescing, and the reverse order would parse the
# header — or admit the request into `_IN_FLIGHT` — off a request the process
# should not have been holding.
app.add_middleware(RequestBodySizeLimit, max_bytes=store.MAX_REQUEST_BYTES)


def _identify(request: Request):
    """(identity, None) or (None, refusal)."""
    ident = identity.resolve(request.headers.get("Authorization"))
    if ident is None:
        return None, refusals.unauthorized()
    return ident, None


def _page(request: Request) -> tuple[int, int, JSONResponse | None]:
    """`limit` and `offset`, bounded. A limit above the cap is CLAMPED rather than
    refused — the caller gets fewer rows and `truncated` tells them there are
    more, which is a better answer than a 400 on a parameter they can't guess the
    ceiling of. A malformed one is refused, because silently reading `"ten"` as a
    default hides a client bug."""
    raw_limit = request.query_params.get("limit", "50")
    raw_offset = request.query_params.get("offset", "0")
    try:
        limit, offset = int(raw_limit), int(raw_offset)
    except ValueError:
        return 0, 0, refusals.refuse(
            400, "limit and offset must be integers",
            limit=raw_limit, offset=raw_offset)
    if limit < 1 or offset < 0:
        return 0, 0, refusals.refuse(
            400, "limit must be at least 1 and offset at least 0")
    if offset > MAX_OFFSET:
        return 0, 0, refusals.refuse(
            400, f"offset may be at most {MAX_OFFSET}",
            hint="narrow the query rather than paging deeper — a clamped offset "
                 "would silently return a different page than the one asked for",
            offset=offset, max_offset=MAX_OFFSET)
    return min(limit, MAX_PAGE), offset, None


def _reject_unknown_params(request: Request, allowed: set[str]) -> JSONResponse | None:
    """400 when the query string carries a name this route does not read,
    naming it and listing what the route DOES read — the unknown-NAME
    counterpart to `order`'s own unknown-VALUE refusal below
    (issue_01M1MTCTH3PW0DWDEXAZA5KFET). A parameter nobody reads is a filter
    that silently never ran, which hands back MORE than the caller asked
    for rather than an empty or malformed result — the wrong direction for
    a route that promises to narrow.

    **This reverses the contract's §3.2 rule that an unrecognised name is
    dropped silently**, a tightening §1 places in version 2. It ships under
    version 1 the way library#201, #247 and #251 did, on a survey of every
    client that found none sending a name its route does not read; the one
    it found, `bin/changelog-drain`'s `since`, became a filter that
    `GET /signals` reads (`_since_clause`).

    `allowed` is each route's own exact set, named at the call site rather
    than inferred, so this cannot drift onto a column it does not actually
    read the way a shared whitelist could.
    """
    unknown = sorted(set(request.query_params.keys()) - allowed)
    if not unknown:
        return None
    return refusals.refuse(
        400, f"unrecognised query parameter: {', '.join(unknown)}",
        hint="a parameter this route does not read is refused rather than "
             "ignored, so a dropped filter cannot silently widen the result",
        unknown=unknown, supported=sorted(allowed))


# The one order a cursor can resume, named once because MINTING a cursor and
# CONSUMING one both have to agree about it. They did not: `next_cursor` was
# minted for every listing while the consumption side refused only an EXPLICIT
# `order=produced_at`, so a client paging `produced_at` and resuming without
# re-sending the parameter got a walk it never asked for — 4 rows repeated and
# 15 of 20 never served, with `next_cursor: null` reporting a clean finish
# (security-reviewer, PR #37). This is `_customer_wall`'s lesson one table over:
# two sites comparing the same literal is the bug, and one definition is the fix.
DEFAULT_SIGNAL_ORDER = "ingested_at"

# The cursor's two halves, joined by a character no ULID or stored timestamp
# contains, so splitting it back is unambiguous without an encoding step a
# client would have to reproduce.
_CURSOR_SEP = "|"


def _cursor_walkable(order_param: str | None) -> bool:
    """Whether a listing in this order can be paged by cursor at all.

    Asked at BOTH ends of the token's life. A `None` order is the default one, so
    omitting the parameter and naming it are the same request — which is exactly
    the equivalence the mint site used to miss.
    """
    return order_param in (None, DEFAULT_SIGNAL_ORDER)


def _signal_cursor(row) -> str:
    """The opaque token that resumes a listing after `row`."""
    return f"{row['ingested_at']}{_CURSOR_SEP}{row['id']}"


def _cursor_clause(raw: str, order_param: str | None) -> tuple[str, list, JSONResponse | None]:
    """A keyset predicate resuming after the row `raw` names.

    **OFFSET PAGING CANNOT REACH THE END OF THIS TABLE, and that is what this
    exists for.** `MAX_OFFSET` refuses an offset past 10,000 — rightly, since a
    clamped offset silently returns a different page than the one asked for — and
    the Archives hold 19,000 rows. Orient's `library_client` pages to completion
    and simply stops with an `HTTPError` partway, so its signals page has been
    rendering a stale static fallback rather than live data
    (`issue_01M06Y897250HGZHFQH2VP1YZS`). A keyset walk has no depth limit,
    because it never counts rows it has passed.

    **It is also correct under concurrent writes, which offset paging is not.** A
    signal captured while a client pages shifts every subsequent offset by one, so
    the client re-reads one row and skips another. The cursor names the last row
    seen, and rows arriving after it simply are not in the walk.

    **Only the default order**, which `_cursor_walkable` decides for this site
    and for the mint site together. `produced_at DESC NULLS LAST, ingested_at
    DESC, id` is a three-column key over a NULLABLE column, and a keyset
    predicate across a NULL boundary is a different and easily-wrong shape. No
    caller pages that order to completion today, so this refuses rather than
    guessing — the refusal names the reason, which a silently wrong page would
    not.
    """
    if not _cursor_walkable(order_param):
        return "", [], refusals.refuse(
            400, "cursor paging is available on the default order only",
            hint="produced_at is nullable, so a keyset walk across it needs a "
                 "different predicate than this one — ask for ingested_at order, "
                 "or narrow the query and use offset",
            order=order_param)
    at, sep, ident_part = raw.partition(_CURSOR_SEP)
    if not sep or not at or not ident_part:
        return "", [], refusals.refuse(
            400, "cursor is malformed",
            hint="pass back the `next_cursor` a previous page returned, "
                 "unchanged — it is opaque and this service composes it",
            cursor=raw)
    # STRICTLY AFTER the named row IN THE LISTING'S OWN ORDER, which is
    # `ingested_at DESC, id` — descending by time and ASCENDING by id within a
    # tie. So the predicate is an earlier instant, or the same instant and a
    # HIGHER id, and getting that backwards is not a subtle failure: a test
    # walking 40 rows written in one second returned 13 and stopped, because the
    # walk asked for the half of each tie group it had already read.
    #
    # Equal timestamps are ordinary rather than exotic — a capture run writes
    # several signals inside one second — so the id half decides whether a page
    # boundary landing mid-tie loses rows.
    return (" AND (ingested_at < ? OR (ingested_at = ? AND id > ?))",
            [at, at, ident_part], None)


def _customer_wall(ident: identity.Identity, alias: str = "") -> tuple[str, list]:
    """The customer-content filter, in SQL, or nothing for a caller that holds
    the capability.

    **One definition, because three sites comparing the same literal is the shape
    the round-1 vulnerability had** (reviewer, PR #4). That bug was five
    independent `== "customer"` comparisons which had to agree and did not; fixing
    the write gate alone left three read-side copies still spelling the class out.
    They are safe today only because the allow-list now guarantees what the column
    can hold — which is a second control covering for a first, rather than the
    consolidation the constant was introduced to be.

    In the query rather than applied afterwards, for the reason the scope clause
    gives: an identity without the capability must not learn such a row exists,
    including through a `total` that counts it. Bound as a parameter so the class
    name appears once in this file.

    `alias` qualifies the column names so this one definition also serves a
    correlated subquery over the same table, where a bare `privacy_class` would
    be ambiguous. **It is a literal from this file and never a request value** —
    it reaches an f-string, so a caller-supplied alias would be an injection
    point, exactly what `SIGNAL_ORDER_CLAUSES` is whitelisted against above. The
    alternative was a second copy of the wall written out at the subquery, which
    is the five-independent-comparisons shape this helper exists to end.
    """
    if identity.authorized(ident, "signal.customer"):
        return "", []
    q = f"{alias}." if alias else ""
    return f" AND ({q}privacy_class IS NULL OR {q}privacy_class != ?)", [
        store.PRIVACY_CUSTOMER]


def _scope_clause(ident: identity.Identity, alias: str = "") -> tuple[str, list]:
    """The product filter, in SQL, so scoping happens in the query rather than in
    a filter after the fact. Filtering after the fact is how a `total` ends up
    counting rows the caller may not see.

    `alias` qualifies the column name for a correlated subquery over the same
    table, on the same terms as `_customer_wall`: a literal from this file, never
    a request value.
    """
    if "*" in ident.products:
        return "", []
    if not ident.products:
        # Authenticated, scoped to nothing. `1 = 0` rather than an early return so
        # every caller takes one code path and `total` is computed the same way.
        return " AND 1 = 0", []
    names = sorted(ident.products)
    q = f"{alias}." if alias else ""
    return f" AND {q}product IN ({','.join('?' * len(names))})", names


def _is_handoff(kind: str | None) -> bool:
    """Whether `kind` names a handoff, case-folded and whitespace-trimmed the
    way `store.SIGNAL_REVIEW_KINDS` folds a protected kind on its way in
    (`kind.lower() in store.SIGNAL_REVIEW_KINDS`, just above the write gate
    below) — so `"Handoff"`, `"HANDOFF"` or a padded `" handoff "` cannot
    bypass a gate keyed on the exact lowercase spelling a compliant writer
    stores today. One function so the write gate and both read gates
    (`_handoff_wall`'s SQL, `_readable_signal`, `_can_return_existing` and
    `_ack_visible`) cannot drift onto different folds of the same test —
    including `_handoff_wall`, which calls this exact function from SQL
    rather than a second, hand-written fold of `kind` (see
    `_register_handoff_sql_functions`, below).

    `.strip()` rather than a space-only trim, so a stored `kind` padded with
    a tab or a non-breaking space folds the same way a space-padded one does
    (reviewer, PR #185 round 3) — the gap `_handoff_wall`'s old
    `TRIM()`-in-SQL version had, `TRIM` stripping only the space character.
    """
    return bool(kind) and kind.strip().lower() == "handoff"


def _private_handoff(product: str | None) -> bool:
    """Whether a `kind = 'handoff'` row for `product` must be hidden because
    the registry marks its canonical product private — `_readable_signal`'s
    single-row counterpart to `_handoff_wall`'s SQL clause, below.

    Resolves `product` the way `create_signal`'s write-side gate does, so a
    row stored under an alias, a repo-name spelling, or with leading/trailing
    whitespace is still caught by its canonical slug — `projects.resolve()`
    strips before comparing. `_handoff_wall` calls this exact function from
    SQL (see `_register_handoff_sql_functions`, below) rather than a second,
    hand-written approximation of its fold, so the two cannot disagree on an
    alias or a whitespace-padded `product` the way round 2's SQL-side
    `private_product_spellings()` match and round 2's un-trimmed SQL
    `product` comparison each did (reviewer + security-reviewer, PR #185
    round 3).

    **Fails CLOSED**, `True` when `projects.private_products()` returns
    `None`: there is then no slug list to clear `product` against, and a
    handoff must be refused rather than shown while the registry cannot say
    whether its product is private — the same inverted trade
    `projects.private_repos()` documents.
    """
    private = projects.private_products()
    if private is None:
        return True
    resolved = projects.resolve(product)
    return bool(resolved) and resolved.strip().lower() in private


def _register_handoff_sql_functions() -> None:
    """Register `_is_handoff` and `_private_handoff` themselves as SQL
    functions on every connection `store.connect()`/`store.connect_rw()`
    open, so `_handoff_wall`'s SQL calls the identical Python code the
    Python-side gates (`_readable_signal`, `_can_return_existing`,
    `_ack_visible`, the write gate) already run on the same row.

    **This is the fix for the defect class both round-2 reviews named**: a
    hand-written SQL fold of `kind`/`product` (`LOWER(TRIM(...))` against a
    precomputed spelling set) is a second definition of `_is_handoff`'s and
    `_private_handoff`'s tests, free to drift from them on an axis nobody
    happened to test — a tab in `kind` (SQLite `TRIM()` strips only the space
    character), or whitespace padding in `product` (never folded at all).
    Calling the real functions from SQL instead of re-deriving their logic
    there means there is only one definition of each test left to call;
    SQL and Python read/write/ack paths agree by construction, not by a
    second approximation kept in sync by hand.

    Run once at import, before any route can open a connection — `store`'s
    registry is a plain module-level list `connect()`/`connect_rw()` consult
    on every open, so a function registered here reaches every connection
    this process opens afterwards, hot and with the cold tier attached
    (SQLite functions are per-connection, not per-schema).
    """
    store.register_connection_function(
        "is_handoff_kind", 1, _is_handoff, deterministic=True)
    # Reads the projects registry, which a reload replaces.
    store.register_connection_function(
        "is_private_handoff_product", 1, _private_handoff)


_register_handoff_sql_functions()


def _handoff_wall(alias: str = "") -> tuple[str, list]:
    """Excludes a `kind = 'handoff'` row for a private product, in SQL, so
    every route that counts or lists signals keeps `total`/`count` honest —
    the same reason `_scope_clause` and `_customer_wall` are SQL clauses
    rather than a filter applied after the fetch.

    **Calls `is_handoff_kind()` and `is_private_handoff_product()`**, the SQL
    functions `_register_handoff_sql_functions` registers above — literally
    `_is_handoff` and `_private_handoff`, run per row by SQLite rather than
    reimplemented as a `LOWER`/`TRIM`/`IN (...)` approximation of them. The
    property this clause has as a result: it excludes exactly the rows
    `_is_handoff(row["kind"]) and _private_handoff(row["product"])` would
    refuse for a single row, because that is the code it runs, not a second
    copy of its reasoning.

    Fails CLOSED like `_private_handoff`: an unreadable registry excludes
    every `handoff`, not none, since there is then no slug list to compare
    `product` against — `_private_handoff` already returns `True` in that
    case, and this clause inherits it rather than restating it.
    """
    q = f"{alias}." if alias else ""
    return (f" AND NOT (is_handoff_kind({q}kind) AND "
            f"is_private_handoff_product({q}product))", [])


def _signals_wall(ident: identity.Identity) -> tuple[str, list]:
    """`_customer_wall` and `_handoff_wall`, combined into the one `wall_fn`
    `_search_vector_page` takes for the Archives half of `GET /search`.

    That route's own wall was `_customer_wall` alone — the handoff wall
    `GET /signals`, `GET /signals/search` and `GET /signals/facets` all
    apply was missing here too, the same gap found on `signal_facets` and
    `_ack_visible` (reviewer and security-reviewer, PR #185 round 1): a
    private product's handoff was rankable and returnable through the
    merged `/search` route even once `GET /signals/search` excluded it.
    """
    wall, wall_params = _customer_wall(ident)
    handoff_wall, handoff_params = _handoff_wall()
    return wall + handoff_wall, wall_params + handoff_params


def _scope_predicate(ident: identity.Identity):
    """`in_scope`, curried for one identity — the lambda `_briefing` builds
    inline, reused everywhere `store.prerequisite_status(es)` resolves a
    prerequisite against a reader's scope rather than the writer's."""
    return lambda product: identity.in_scope(ident, product)


def _since_clause(request: Request):
    """`(clause, params, None)` for `since`, the ISO-8601 lower bound on
    `produced_at` that `GET /signals` and `GET /signals/facets` both take, or
    `(None, None, refusal)` for a value that is not a timestamp with a zone.

    `bin/changelog-drain` (agent-platform) sent `since` on every run before
    any route read it, so each pass read the whole changelog history instead
    of its window (issue_01M1MTCTH3PW0DWDEXAZA5KFET). The bound is inclusive.

    **The zone is required** because `produced_at` is stored as naive UTC,
    so a naive bound would silently mean whatever zone its sender assumed.
    It is converted to UTC and written in the stored shape, with
    microseconds only when it has them, which compares correctly as text
    against both stored shapes. A row with no `produced_at` never matches.
    """
    raw = request.query_params.get("since")
    if raw is None:
        return "", [], None
    try:
        bound = _dt.datetime.fromisoformat(raw.strip())
    except ValueError:
        bound = None
    if bound is None or bound.tzinfo is None:
        return None, None, refusals.refuse(
            400, "since must be an ISO-8601 timestamp with a zone",
            hint="for example 2026-10-11T13:00:00Z", since=raw)
    bound = bound.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    shape = "%Y-%m-%d %H:%M:%S.%f" if bound.microsecond else "%Y-%m-%d %H:%M:%S"
    return " AND produced_at >= ?", [bound.strftime(shape)], None


def _signal_filters(request: Request, where: str, params: list,
                    ident: identity.Identity) -> tuple[str, list]:
    """The `kind`, `source`, `stream`, `product`, `repo` and `unreferenced_by`
    filters `GET /signals` and `GET /signals/facets` both apply, so the two
    cannot drift the way the customer wall's five independent `== "customer"`
    comparisons once did (PR #4). `kind`, `source`, `stream` and `repo` are
    repeatable (`?kind=a&kind=b` → `IN (a, b)`); `product` is not, since the
    scope clause already bounds it to one caller's products and every other
    list route in this file takes a single `product`. `repo` has no such
    bound: several platform-owned repositories share product `platform`, so a
    panel drawing rows from more than one of them would need one call per
    repository if `repo` were singular too. `stream` is open the same way
    `kind` is — a caller narrowing to one audit stream today may need two
    tomorrow, and there is no scope clause bounding it the way there is for
    `product`.

    **`repo` matches `meta.repo`, a fixed JSON path written into the query as a
    literal from this file, never from the request** — the same reasoning
    `unreferenced_by` gives below for keeping a JSON path out of the query where
    a caller-supplied one would be an injection point.

    **No `json_valid` guard on `meta`, unlike the `refs` reads below, because
    this service writes every `meta` with `json.dumps`.** `json_extract` raises
    only on text that is not JSON at all; on a JSON value that is not an object
    it returns NULL, which a `repo` filter simply fails to match. So the guard
    is unnecessary exactly as long as nothing stores `meta` without
    serializing it. `frontmatter_extra` needs its guard because another
    repository writes it.

    **This deliberately names no writers and gives no count.** Two earlier
    drafts listed them and both undercounted — one grep was cut short by
    `head`, and a route that writes through a helper is invisible to a search
    for the helper's callers alone. A list here goes stale the day a writer is
    added. The invariant is checked instead, from the repository root:

        grep -rnE "insert_signal[(,]|SET meta" --include="*.py" . | grep -v tests/

    Every hit must build `meta` with `json.dumps`. On 2026-09-16 all of them
    did, and all 48,545 stored rows passed `json_valid`. **A writer that
    stores `meta` any other way must add this guard, in the same change.**

    **`unreferenced_by=<kind>` keeps the rows that no signal of that kind points
    at**, which is what "which proposals are still live" asks for:
    `?kind=proposal&unreferenced_by=proposal_decision`. Without it a client
    fetches every proposal and every decision for a product and joins them
    itself, and Deck would carry that join in `LibraryClient` — the "function
    grown inside a client because its view needed one" its own bead rules out.

    **It matches a row's id anywhere in the referencing row's `refs`, rather than
    at an agreed key.** `kind` is an open vocabulary with no closed set, so the
    convention for where a decision records its proposal can drift to a second
    spelling in a second writer; a key-path filter would then return fewer rows
    than exist and look like an answer. Reading every top-level `refs` value
    cannot drift that way, and it keeps a JSON path out of the query, where a
    caller-supplied one would be an injection point.
    """
    if (kinds := request.query_params.getlist("kind")):
        where += f" AND kind IN ({','.join('?' * len(kinds))})"
        params += kinds
    if (sources := request.query_params.getlist("source")):
        where += f" AND source IN ({','.join('?' * len(sources))})"
        params += sources
    if (streams := request.query_params.getlist("stream")):
        where += f" AND stream IN ({','.join('?' * len(streams))})"
        params += streams
    if (product := request.query_params.get("product")):
        where += " AND product = ?"
        params.append(product)
    if (repos := request.query_params.getlist("repo")):
        where += (" AND json_extract(meta, '$.repo') IN "
                  f"({','.join('?' * len(repos))})")
        params += repos
    if (unref := request.query_params.get("unreferenced_by")):
        # THE SUBQUERY TAKES THE SAME SCOPE AND THE SAME WALLS, and that is a
        # disclosure decision rather than tidiness. A decision row the caller may
        # not read must not remove a proposal from its listing: absence would say
        # a decision exists, which is the thing the walls and the scope refuse to
        # say. So the caller sees a set consistent with what it can see, and a
        # proposal decided out of its scope reads as live to it.
        #
        # **THE HANDOFF WALL BELONGS HERE FOR THE SAME REASON** (PR #185 round
        # 2): `?kind=X&unreferenced_by=handoff` without it would let a private
        # product's handoff count as a referencing row, silently removing the
        # row it references from an "unreferenced" listing — disclosing that
        # an invisible row references it, exactly the leak the comment above
        # already refuses for an unreadable decision.
        ref_scope, ref_scope_params = _scope_clause(ident, "ref")
        ref_wall, ref_wall_params = _customer_wall(ident, "ref")
        ref_handoff, ref_handoff_params = _handoff_wall("ref")
        # **`CASE WHEN` RATHER THAN `json_valid(...) AND EXISTS(...)`**, which is
        # the third site in this file to need the rule and the second to state
        # it. `json_each` over an unparseable blob aborts the whole statement
        # with `malformed JSON` — SQLite does not skip the bad row — so one such
        # row anywhere in `signals` would take down every listing that passes
        # this filter, not merely its own. Guarding with `AND` relies on the
        # planner applying the predicate before the table-valued function, which
        # nothing guarantees; `CASE` is defined to evaluate its branches lazily,
        # so the guard cannot be optimised away. `reference_topics` and the
        # `audience` filter reached the same conclusion by the two routes
        # available, a pre-filtering subquery and this one.
        #
        # A NULL `refs` takes the ELSE branch and matches nothing, since
        # `json_valid(NULL)` is NULL rather than true — the same answer
        # `json_each` would give it, without a separate IS NOT NULL test.
        #
        # **NO `json_type` RESTRICTION, unlike the `audience` filter**, and the
        # difference is the question being asked. There, an object iterating its
        # VALUES made `{"a": "architect"}` match an audience that was never
        # `["architect"]` — a wrong answer with no shape reporting it. Here any
        # shape carrying the id IS a reference to it, so there is no shape that
        # produces a wrong answer to narrow away.
        # **THE REF SIDE READS BOTH TIERS TOO** (archives-cold-tier.md §1 row
        # 5) — a cold `proposal_decision` must still retire a hot
        # `proposal`, and the caller's own connection must already have
        # `cold` attached by the time this runs, since the subquery
        # references it unconditionally. `store.signals_source()` is
        # unaliased; `AS ref` here gives it the alias this subquery's own
        # correlated references (`ref.kind`, `ref.refs`) already use.
        #
        # **THE HANDOFF WALL APPLIES TO BOTH TIERS OF THE REF SIDE TOO**: a
        # cold `handoff` row for a private product must not count as a
        # referencing row any more than a hot one may.
        ref_source = store.signals_source()
        where += (f" AND NOT EXISTS (SELECT 1 FROM {ref_source} AS ref "
                  f"WHERE ref.kind = ?{ref_scope}{ref_wall}{ref_handoff} "
                  "AND (CASE WHEN json_valid(ref.refs) "
                  "THEN EXISTS (SELECT 1 FROM json_each(ref.refs) "
                  "WHERE json_each.value = signals.id) ELSE 0 END))")
        params += [unref, *ref_scope_params, *ref_wall_params, *ref_handoff_params]
    return where, params


# WHITELISTED AGAINST A SET OF COLUMN NAMES, NEVER INTERPOLATED FROM THE RAW
# PARAM. `order` reaches an f-string that builds the query, so accepting
# anything but a name from this table would make the query parameter an
# injection point. `produced_at` sorts NULLs last under DESC even without the
# explicit clause (SQLite's own rule), but the clause names it anyway to match
# `release_notes_build`'s ordering exactly rather than resting on that default.
#
# EVERY CLAUSE ENDS IN `id`, and so does every other ordered query in this file.
# SQLite leaves the order among tied rows unspecified, so a sort on one column
# makes the determinism the contract promises a property of the implementation
# rather than of the query — and under LIMIT/OFFSET that is the shape which lets
# a tied row land on two pages or on neither. The primary key is unique, so
# appending it makes the total order total.
SIGNAL_ORDER_CLAUSES = {
    # Written as the constant rather than the literal, so the default order has
    # exactly one spelling. This was the last site holding its own copy, and a
    # rename that missed it would `KeyError` on every unordered listing.
    DEFAULT_SIGNAL_ORDER: "ingested_at DESC, id",
    "produced_at": "produced_at DESC NULLS LAST, ingested_at DESC, id",
}


# `/health` needs semantic search's CURRENT readiness, not the boot-time snapshot
# `lifespan` logs once. On 2026-08-24 this repo's `.venv` was deleted out from
# under the running process; `vectors.available()` started returning False on
# every call, and `GET /signals/search` refused with 503 for twenty minutes
# while `/health` kept answering ready, because it read no signal at all. A
# watchdog polling only `/health` reported the service up the whole time.
#
# **CACHED, with a TTL, and refreshed off the request path.** `vectors.available()`
# opens a fresh SQLite connection and runs a query, so a watchdog polling
# `/health` every second would turn a liveness probe into steady database load
# if it recomputed per call — and, measured 2026-08-26 against the running
# service, that recompute itself took 2.8-4.3s in service against single-digit
# milliseconds out of process, which made `/health` itself an intermittent false
# "down" for any caller with a timeout under that. `_readiness_refresh_loop`
# recomputes on this cadence from a background task instead, so `health()` only
# ever reads whatever `_readiness_cache` already holds — see there, and see
# `_readiness_snapshot`. A cache is correct HERE specifically because the bug
# this fixes is staleness measured in a DAY (one boot-time line that never
# re-ran); staleness bounded to a few seconds is a different thing and a real
# fault still surfaces within one TTL.
_READINESS_TTL_SECONDS = 15

# **A DEAD OR STUCK REFRESHER MUST NOT FREEZE `ready: true` FOREVER** — that is
# the 2026-08-24 failure above, recurring in a new shape: instead of "the
# boot-time snapshot never re-runs", it would be "the background loop that
# replaced it stopped running". `_readiness_snapshot` answers not-ready once
# the cache is older than this bound, so a refresher that is stuck or dead
# surfaces as down rather than reporting a ready that nothing is checking
# anymore. Three TTL windows: enough slack that one missed cycle to ordinary
# lock contention (see `_READINESS_CONNECT_TIMEOUT_SECONDS` below) does not
# flip a healthy service to not-ready, while still catching a refresher that
# is genuinely gone within 45s rather than never.
_READINESS_STALE_AFTER_SECONDS = 3 * _READINESS_TTL_SECONDS

# **THE WORST CASE IS SECONDS, NOT MILLISECONDS, and it is bounded per busy-wait,
# not per call.** Once `store.connect_rw` has switched the database to WAL
# (issue_01M2KW35JX19GVG9Z13R10HKNZ), `vectors.connect()` is never forced to wait
# behind an ordinary writer's commit — only behind a writer's own EXCLUSIVE lock
# (a checkpoint, a `VACUUM`), which is rarer and still bounded by this same
# timeout. sqlite3 passes `timeout` to `sqlite3_busy_timeout()`, which re-arms on
# every statement that hits the lock on that connection, and one readiness
# recompute hits it at two independent points: opening the connection (which
# also loads the `vec0` extension) and then running the `SELECT COUNT(*)`. So
# this value bounds each wait, not the recompute as a whole — the worst case for
# one call is closer to 2x this number, about 2 seconds. That cost now lands on
# `_readiness_refresh_loop`'s own threadpool call, off the request path
# entirely, rather than tying up a caller's request for that long — worth
# eating an occasional false "not ready" on real contention either way, on a
# background cycle rather than in front of anyone.
_READINESS_CONNECT_TIMEOUT_SECONDS = 1.0

_readiness_cache: tuple[float, bool, str] | None = None


def _readiness_snapshot() -> tuple[bool, str]:
    """The readiness `(ready, reason)` the background refresh loop last
    computed — a pure read of `_readiness_cache`, no I/O, safe to call
    directly on the event loop.

    Reads `_readiness_cache` into a local exactly once and indexes the local
    from there. Reading the global separately per field would reopen the
    torn-read hazard this closes: each attribute access compiles to its own
    `LOAD_GLOBAL`, and a recompute on another thread can rebind the name
    between any two of them, handing a caller a `ready` from one generation
    paired with a `reason` from another that never coexisted.

    **Two cases answer `False` rather than passing through whatever is
    cached, or isn't, because a stale or absent `True` would be exactly the
    2026-08-24 failure this design exists to end:**

    - `_readiness_cache` is `None`. In a running deploy this cannot happen —
      ASGI does not route a request to this process until `lifespan`'s
      startup has returned, and startup seeds the cache before it returns —
      but a test, or any other caller that drives `health()` without running
      `lifespan`, can reach this, and it must get a defined answer rather
      than an `AttributeError` on an unpacked `None`.
    - The cached entry is older than `_READINESS_STALE_AFTER_SECONDS`. That
      means `_readiness_refresh_loop` has missed several cycles or stopped
      running outright, and nothing has actually checked readiness in that
      window — see `_READINESS_STALE_AFTER_SECONDS` for why that must not
      report a frozen `ready: true`.
    """
    cache = _readiness_cache
    if cache is None:
        return False, "readiness has not been checked yet"
    checked_at, ready, reason = cache
    if time.monotonic() - checked_at > _READINESS_STALE_AFTER_SECONDS:
        return False, "the readiness cache is stale; the background refresh may be stuck or dead"
    return ready, reason


def _semantic_search_readiness() -> tuple[bool, str]:
    """Recompute readiness from `vectors.available()` and rebind
    `_readiness_cache` to the result, stamped with the time of this check.

    **Must never raise.** `/health` is the liveness probe; a probe that can 500
    or hang is worse than no probe at all, so any exception here becomes a
    not-ready answer naming the exception type rather than propagating.

    **Called from exactly two places, never from the request path.**
    `_readiness_refresh_loop` calls this on its `_READINESS_TTL_SECONDS`
    cadence, through `run_in_threadpool`, because `vectors.connect()` can
    block on SQLite's lock wait for up to `_READINESS_CONNECT_TIMEOUT_SECONDS`
    twice over, and that must never stall the loop that keeps every other
    request's readiness answer warm. `lifespan` also calls this once,
    directly on the event loop at startup, to seed the cache before the
    first request can arrive — see there. `health()` itself never calls
    this; it only reads whatever `_readiness_cache` already holds, through
    `_readiness_snapshot`.

    **No lock guards `_readiness_cache`, and that is still correct off the
    event loop.** The only write is `_readiness_cache = (...)`, one name
    rebind to a fully-built, immutable tuple; CPython's GIL makes that
    rebind atomic, and `_readiness_snapshot`'s single local binding makes
    every read see either the old tuple or the new one, whole, never a mix
    of fields from both. The two callers above never run concurrently with
    each other — the refresh loop sleeps before its first tick specifically
    so it cannot race the startup seed — so there is not even a redundant
    write to converge on here, unlike when callers on the request path could
    race each other past the TTL boundary.

    Not ready is logged here, once per recomputation. Recomputation now
    happens once per `_READINESS_TTL_SECONDS`, from the refresh loop, rather
    than once per cache miss on the request path, so this is still a fault
    reaching the service log on its own schedule rather than a healthy
    service writing a line on every request.
    """
    global _readiness_cache
    now = time.monotonic()
    try:
        ready, reason = vectors.available(
            store.DB_PATH, timeout=_READINESS_CONNECT_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — health must never fail
        ready, reason = False, f"readiness check raised {type(exc).__name__}"
    if not ready:
        print(f"library: semantic search not ready — {reason}", file=sys.stderr)
    _readiness_cache = (now, ready, reason)
    return ready, reason


async def _readiness_refresh_loop() -> None:
    """Recompute semantic-search readiness on `_READINESS_TTL_SECONDS`
    cadence, so `_readiness_cache` is never more than one cycle stale and
    `health()` never recomputes on a caller's request path. See
    `_semantic_search_readiness` for what gets rebound and why that is safe
    with no lock.

    **Sleeps before its first check, rather than checking then sleeping**,
    because `lifespan` already performs the first check directly, to seed
    the cache before any request can arrive. Checking again immediately
    here would be a redundant recompute racing nothing.

    Runs as a background task started in `lifespan` and cancelled on
    shutdown, exactly like `_restart_watcher` beside it; never touches the
    request path.

    **Must never die.** A task that raises escapes `asyncio.create_task`
    silently — Python logs "Task exception was never retrieved" once and the
    task simply stops running. `_readiness_cache` would then freeze at
    whatever it last held, aging past `_READINESS_STALE_AFTER_SECONDS` and
    reporting not-ready forever rather than resuming — the dead-refresher
    case `_READINESS_STALE_AFTER_SECONDS` exists to catch only works if
    something is still around to have gone stale from checking at all.
    `_semantic_search_readiness` already turns an exception from the check
    itself into a not-ready result rather than raising; this catches
    anything else so the loop keeps running regardless of what the check
    does.
    """
    while True:
        await asyncio.sleep(_READINESS_TTL_SECONDS)
        try:
            await run_in_threadpool(_semantic_search_readiness)
        except Exception as exc:  # noqa: BLE001 — this loop must never die
            print(f"library: readiness refresh loop raised "
                  f"{type(exc).__name__} — continuing", file=sys.stderr)


@app.get("/health")
async def health(request: Request):
    """Liveness, the protocol versions this build serves, and whether semantic
    search can actually serve right now. Deliberately unauthenticated and exempt
    from negotiation — it answers "is this process up and what does it speak",
    which is what a client needs BEFORE it can form a valid request.

    **`semantic_search.ready` is not the same fact `lifespan` logs at boot.**
    That line is true once, at process start; this reads `_readiness_cache`,
    which `_readiness_refresh_loop` keeps warm on a fixed cadence for as long
    as the process runs — the difference the 2026-08-24 incident turned on.

    **Never recomputes here, on the caller's own request.** Measured
    2026-08-26 against the running service, a caller-pays recompute took
    2.8-4.3s in service against single-digit milliseconds measuring the same
    check out of process — an intermittent false "down" for any watchdog with
    a shorter timeout, on the one route whose entire job is answering that
    question correctly. This route only ever reads whatever
    `_readiness_snapshot` returns; see there for what it answers before the
    first refresh completes and when the cache has gone stale.

    **`commit` is the git commit this PROCESS loaded, and is absent when it
    could not be resolved.** It is stamped at import rather than read per
    request — see `RUNNING_COMMIT` for why that distinction is the whole point.
    Absent rather than null, so a client that needs it says so instead of
    rendering "unknown" as though it were an answer. It is unauthenticated on
    the same terms as `protocol`: the caller who most needs it is one deciding
    whether it is talking to the build it expects, before it can form an
    authenticated request.

    **`staged_commit` is what is on disk right now, read fresh on every
    call** — see `_staged_commit` for why that is the opposite choice from
    `commit`, deliberately. Once a deploy area updates on merge but this
    process has not yet restarted onto it, `commit` and `staged_commit`
    disagree, and that disagreement is the whole point of reporting both:
    the drift is legible here instead of being discovered the way the
    2026-08-20 incident was. Same contract as `commit`: absent, not null,
    when it cannot be resolved — which is always, for a working checkout.

    **`restart` reports the quiesce state platform/decisions/0059 asks for**:
    whether this process is currently refusing new work pending a restart,
    and how many requests it is still waiting to drain. Always present —
    `quiescing` is plain internal state, not a value that can go
    unresolvable the way a commit hash can, so there is nothing here for the
    absent-rather-than-null convention to apply to.

    **`reason` is never in the response, for any caller, authenticated or not.**
    Two of `vectors.available()`'s three failure strings name only an exception
    type, but the third interpolates a `sqlite3.OperationalError`'s message
    verbatim, and nothing bounds what a future SQLite or sqlite-vec version puts
    in that text. The database-misconfiguration refusal above (convivy/library#32)
    keeps exactly this kind of detail out of every response and puts it in the
    service log instead; an earlier version of this route gated `reason` on
    authentication and called that the same precedent, which it was not — that
    handler withholds its detail from every caller, not just unauthenticated
    ones, and the lowest-trust `agent` credential kind resolves here with no role
    check. This route now follows the precedent it cites: the boolean is public,
    the reason goes to the log, and `_semantic_search_readiness` prints it there
    when the background refresh finds the service not ready.
    """
    if (err := _reject_unknown_params(request, set())):
        return err
    ready, _ = _readiness_snapshot()
    semantic_search = {"ready": ready}
    body = {"status": "ok", "protocol": {"current": protocol.CURRENT,
                                         "supported": list(protocol.SUPPORTED)},
            "semantic_search": semantic_search}
    # **Absent rather than null when it cannot be resolved.** A `commit: null`
    # invites a client to render "unknown" as though it were an answer; an absent
    # key makes a client that needs it say so. `RUNNING_COMMIT` is stamped at
    # import — see its definition for why never per request.
    if RUNNING_COMMIT:
        body["commit"] = RUNNING_COMMIT
    # Live, not cached — see `_staged_commit` for why this one is read fresh
    # on every call while `commit` above never is.
    staged = _staged_commit()
    if staged:
        body["staged_commit"] = staged
    body["restart"] = {"quiescing": _QUIESCING, "in_flight": _IN_FLIGHT}
    return body


@app.get("/whoami")
async def whoami(request: Request):
    """The calling identity as the Library resolved it, so a client can assert on
    its own grant instead of assuming it.

    **A MISPROVISIONED CREDENTIAL IS OTHERWISE SILENT, AND SILENT IN THE DIRECTION
    THAT LEAKS.** ADR-0022 gives the privacy wall to this service and Orient no
    longer filters `privacy_class` itself, which is right — a duplicate client-side
    filter works against the design. The cost is that whether a full-tier page
    renders customer-class signals now hangs on one JSON key: `"kind": "system"` on
    Orient's entry in a gitignored token file no code review sees. Nothing in the
    request path fails when that key is wrong; the page simply renders more than it
    should. `convivy-orient#152` is the precedent, where a tier check in the wrong
    layer served 16,000 internal records to a second caller.

    So the fix is not another wall. It is that the caller can READ the grant it was
    given and refuse to serve when it is wider than it expects — which needs
    `identity.resolve`'s four fields to be reachable, and until now no route
    exposed them. Nothing here is computed; this is a window onto a resolution that
    already happened.

    **Authenticated like every other route, and gated on no capability.** An
    unauthenticated caller must not learn what a token would grant, so a missing or
    unknown credential is the ordinary 401. Past that there is nothing to authorize
    against: the answer is the caller's own identity, and a capability check would
    mean a credential could be too narrow to discover how narrow it is — which is
    the position the client is trying to get out of.

    **It reports the CALLER and takes no argument.** A query parameter is refused
    rather than ignored, because ignoring one answers a question nobody asked: a
    client author writing `?actor=orient` to inspect another identity would get 200
    with their OWN capabilities and conclude they had checked Orient's. That is the
    same wrong-answer-with-a-200 shape this route exists to remove, so it must not
    be reintroduced by the route itself.
    """
    ident, err = _identify(request)
    if err:
        return err
    if request.query_params:
        return refusals.refuse(
            400, "/whoami takes no parameters",
            hint="it reports the calling identity only. There is no route for "
                 "looking up another identity, and there will not be one — a "
                 "client that could enumerate grants is a client that can find "
                 "the widest one.",
            got=sorted(request.query_params.keys()))
    return {"actor": ident.actor, "kind": ident.kind,
            "products": sorted(ident.products),
            "capabilities": sorted(ident.caps)}


@app.get("/whoami/capabilities")
async def whoami_capabilities(request: Request):
    """The caller's capability set and where each member comes from: the role,
    or the credential's own entry, plus what the entry withheld. Authenticated
    and ungated like `/whoami`, reports the CALLER only, and takes no argument.

    **A separate route rather than more fields on `/whoami`**, whose body
    clients compare whole; adding a key there would break a caller that asserts
    on the exact shape it was promised.
    """
    ident, err = _identify(request)
    if err:
        return err
    if request.query_params:
        return refusals.refuse(
            400, "/whoami/capabilities takes no parameters",
            hint="it reports the calling credential only.",
            got=sorted(request.query_params.keys()))
    return {"actor": ident.actor, **identity.capability_report(ident)}


# **THE SORT IS AN ALLOWLIST OF FRAGMENTS, NEVER AN INTERPOLATED COLUMN NAME.**
# `sort` reaches SQL as part of `ORDER BY`, where a bind parameter cannot go, so
# the only safe shape is a fixed map from a caller-supplied key to a fragment
# this file wrote. Nothing derived from the request is ever concatenated.
#
# `topic` orders by an item's FIRST topic, which is what makes a queue readable
# one area at a time — the whole point of the field. Untopiced items sort last
# rather than first: they are the residue of a grouping pass, and burying the
# grouped work under them would defeat the read the sort exists for.
#
# The same `json_valid`/`json_type` guard the filter carries, for the same
# reason: `json_extract` on a malformed column would take the page down.
_ITEM_TOPIC_KEY = ("CASE WHEN json_valid(topics) AND json_type(topics) = 'array' "
                   "THEN json_extract(topics, '$[0]') END")
# **NO `COLLATE NOCASE`, and the first version of this had one.** The filter
# matches with `value = ?` and dedupe is case-sensitive, both deliberately, so a
# case-folding sort would be a third behaviour for one field. `DESIGN` and
# `design` ARE two topics here; sorting them apart is the truthful answer, and it
# makes an accidental duplicate visible rather than hiding it adjacent to its
# twin — which is the failure someone regrouping a queue most needs to see
# (reviewer, PR #99).
ITEM_SORTS = {
    "created_at": "created_at DESC, id",
    "topic": (f"({_ITEM_TOPIC_KEY}) IS NULL, ({_ITEM_TOPIC_KEY}), "
              f"created_at DESC, id"),
}


def _item_order(request: Request):
    """The ORDER BY fragment for this request. Returns `(order, refusal)`.

    **AN UNKNOWN SORT IS A 400, NOT A FALLBACK TO THE DEFAULT.** This route has
    now shipped three filters that were silently ignored — `claimable`, `needs`
    and `topics` — and the `needs` note records why that direction is the
    dangerous one: a wider answer reads as a working call. A caller asking for
    `sort=priority` and receiving a newest-first page has no way to tell it did
    not happen.
    """
    sort = request.query_params.get("sort")
    if sort is None:
        return ITEM_SORTS["created_at"], None
    if sort not in ITEM_SORTS:
        return None, refusals.refuse(
            400, f"sort must be one of {sorted(ITEM_SORTS)}", got=sort)
    return ITEM_SORTS[sort], None


@app.get("/items")
async def list_items(request: Request):
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "item.read"):
        return refusals.forbidden("item.read")
    if (err := _reject_unknown_params(request, {
            "limit", "offset", "state", "product", "type", "needs",
            "parent_id", "found_during", "home", "topics", "sort"})):
        return err
    limit, offset, err = _page(request)
    if err:
        return err
    where, params = "WHERE 1 = 1", []
    scope, scope_params = _scope_clause(ident)
    where += scope
    params += scope_params
    state = request.query_params.get("state")
    if state == store.ITEM_STATE_CLAIMABLE:
        # **`claimable` IS DERIVED, WHICH IS WHY MATCHING IT AGAINST THE COLUMN
        # RETURNED NOTHING.** No row holds the string, so the plain `state = ?`
        # this route used to run answered 0 where Orient answers 258 — and
        # `issue_client.list_claimable_issues` is the queue drain's entry point,
        # so at the cutover the drain would have seen an empty queue. Its
        # ARCH-362 guard fires on exactly that shape, which is the only reason
        # this was caught before the window rather than in it.
        #
        # Three clauses, and each one is load-bearing:
        where += (f" AND (state = ? OR (state = ? AND "
                  f"{(claimable := lease.claimable_clause())[0]}))")
        params += [store.ITEM_STATE_OPEN, store.ITEM_STATE_CLAIMED, *claimable[1]]
        # An operator-only item is never agent-claimable. A drain that takes one
        # cannot finish it, and the item then sits claimed with no outcome —
        # invisible to the next pass for a full TTL, then claimed and abandoned
        # again. `IS NULL` is required rather than decorative: NULL is a row
        # predating the column and reads as the `agent` default everywhere else,
        # and SQL's three-valued logic makes `!= 'operator'` false for NULL, so
        # without it every pre-column item would vanish from the drain's queue.
        where += " AND (needs IS NULL OR needs != ?)"
        params.append(store.ITEM_NEEDS_OPERATOR)
        # A BLOCKED ITEM IS NEVER CLAIMABLE (the operator, 2026-09-10). In the SQL rather
        # than filtered after the fact, matching `_scope_clause`'s own reasoning:
        # filtering after the query would leave `total` counting rows a drain
        # would immediately be refused for, the same wrong answer an out-of-band
        # scope filter gives.
        where += f" AND NOT {(blocked := store.blocked_clause())[0]}"
        params += blocked[1]
    elif state:
        where += " AND state = ?"
        params.append(state)
    if (product := request.query_params.get("product")):
        where += " AND product = ?"
        params.append(product)
    if (type_ := request.query_params.get("type")):
        # **NOT VALIDATED, matching the contract's DEFAULT clause** (§3.2 of
        # projects/platform/contracts/library-protocol.md): omitting `type`
        # already returns every type, so no caller needs a sentinel, and an
        # unknown value is answered with whatever rows carry it — usually an
        # empty page — rather than refused.
        where += " AND type = ?"
        params.append(type_)
    # **`needs` WAS DROPPED ON THE FLOOR, which is worse than `claimable` was.**
    # An unrecognised filter is ignored by this route, so `?needs=operator`
    # returned every open item as though nothing had been asked — 275 rows where
    # Orient returns 2. A narrower answer is a visible failure; a wider one reads
    # as a working call, and `issue-queue-drain`'s operator watch would have
    # reported the whole queue as waiting on the operator.
    if (needs := request.query_params.get("needs")):
        if needs not in store.ITEM_NEEDS:
            # 400, matching `state must be live | all` on the leases route and
            # Orient's own `needs` check. The contract reserves 422 for the §5
            # revision validation rather than for query-parameter filtering, and
            # `refusals.py`'s table paraphrases that in a way broad enough to
            # mislead — this was the only 422 in the service (reviewer,
            # convivy/library#34).
            return refusals.refuse(
                400, f"needs must be one of {sorted(store.ITEM_NEEDS)}", got=needs)
        if needs == store.ITEM_DEFAULT_NEEDS:
            # The default is stored as NULL on rows predating the column, so
            # asking for it has to match both spellings or the filter hides
            # history.
            where += " AND (needs IS NULL OR needs = ?)"
        else:
            where += " AND needs = ?"
        params.append(needs)
    if (parent := request.query_params.get("parent_id")):
        where += " AND parent_id = ?"
        params.append(parent)
    if (found_during := request.query_params.get("found_during")):
        where += " AND found_during = ?"
        params.append(found_during)
    if (home := request.query_params.get("home")) is not None:
        if home != "invalid":
            return refusals.refuse(
                400, f"home must be 'invalid' when present, not {home!r}",
                hint="the only recognised value surfaces a non-epic row "
                     "whose parent names neither an epic nor a corrections "
                     "queue in its own product")
        # **A NON-EPIC ROW WITH NO VALID HOME** (`library/specs/one-home-
        # per-work-item`, PR 2). `create_item`, `insert_item` and `/correct`
        # all guarantee a non-epic's `parent_id` names an epic or a
        # corrections queue in its own product, and PR 2's migration
        # backfilled every row filed before they did — so this is the net
        # for what slips past all of that: a NULL parent, or one naming
        # neither table here, which only a direct insert outside those
        # paths can still leave (`replay_stranded.py`, `migrate.py`;
        # `library/decisions/0003`).
        where += " AND (type IS NULL OR type != ?)"
        params.append(store.ITEM_TYPE_EPIC)
        where += (
            " AND (parent_id IS NULL OR ("
            "NOT EXISTS (SELECT 1 FROM issue_items AS p WHERE p.id = "
            "issue_items.parent_id AND p.type = ? AND p.product = "
            "issue_items.product) "
            "AND NOT EXISTS (SELECT 1 FROM corrections_queues AS q WHERE "
            "q.id = issue_items.parent_id AND q.product = "
            "issue_items.product)))")
        params.append(store.ITEM_TYPE_EPIC)
    # **`topics` WAS THE THIRD FILTER THIS ROUTE DROPPED ON THE FLOOR**, and it
    # failed the way the `needs` note above calls the worse one: `?topics=DESIGN`
    # returned all 145 open vivy items rather than none, so a caller reading a
    # grouped queue got the whole queue and no error.
    #
    # **`CASE WHEN` RATHER THAN `EXISTS(...) AND json_valid(...)`**, which makes
    # this the fourth site in this file to need the rule. `json_each` over an
    # unparseable blob aborts the whole statement with `malformed JSON` — SQLite
    # does not skip the bad row — so one such row anywhere in `issue_items` would
    # take down every listing passing this filter, not merely its own.
    #
    # THE FIRST VERSION OF THIS FILTER USED THE `AND` CHAIN, and it worked. That
    # is the problem: guarding with `AND` relies on the planner applying the
    # predicate before the table-valued function, which nothing guarantees, and
    # on SQLite 3.53.3 the planner happens to do it. `CASE` is defined to
    # evaluate its branches lazily, so the guard cannot be optimised away.
    # `unreferenced_by`, `GET /reference/topics` and the `audience` filter all
    # moved off the `AND` shape for exactly this reason; this one reintroduced it
    # (reviewer, PR #99).
    #
    # `json_type` IS CHECKED AS WELL AS `json_valid`, unlike `unreferenced_by`.
    # There, any shape carrying the id is a reference to it. Here an object would
    # iterate its VALUES, so `{"area": "DESIGN"}` would match a topic the item
    # does not have — a wrong answer with nothing reporting it, which is the
    # `audience` filter's case rather than `refs`'.
    if (topic := request.query_params.get("topics")):
        where += (" AND (CASE WHEN json_valid(topics) "
                  "AND json_type(topics) = 'array' "
                  "THEN EXISTS (SELECT 1 FROM json_each(topics) "
                  "WHERE json_each.value = ?) ELSE 0 END)")
        params.append(topic)
    order, err = _item_order(request)
    if err:
        return err
    conn = store.connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM issue_items {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT {store.ITEM_COLUMNS} FROM issue_items {where} "
            f"ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, limit, offset]).fetchall()
        # ONE QUERY FOR THE WHOLE PAGE'S CLAIM EXPIRIES, rather than one per
        # claimed row. The expiry lives on the lease, so a serialiser fetching it
        # itself is an N+1 on the busiest read route this service has.
        claims = lease.claim_statuses(conn, rows)
        prereqs = store.prerequisite_statuses(conn, rows,
                                              in_scope=_scope_predicate(ident))
    finally:
        conn.close()
    return {"items": [store.item_dict(
                          r, claim=claims.get(r["id"], (False, None)),
                          prerequisites=prereqs[r["id"]])
                      for r in rows],
            "count": len(rows), "total": total, "offset": offset,
            "truncated": offset + len(rows) < total}


def _thread(conn, item_id: str) -> list[dict]:
    """One item's comment thread, oldest first.

    **The Library could WRITE this thread and never read it back**, which the
    first shadow run against live data surfaced: `POST /items/{id}/comments`
    existed, `comments` appeared in no response, and no route served them. A
    diagnosis a fixer is supposed to read had nowhere to be read from, and
    `/revision`'s adjacency briefing — recorded on the item precisely so a later
    reader sees what the holder was told — was write-only with it.

    Oldest first, because a thread is a conversation and reading it backwards
    makes a reply arrive before what it answers. The API's own cap bounds it at
    `MAX_COMMENTS_PER_ITEM`, so there is nothing to paginate that the write path
    has not already limited.

    **A COMMENT CARRIES NO PRIVACY CLASS, AND NEITHER DOES THE ITEM IT HANGS OFF**
    (security-reviewer, PR #20; `issue_01KZPKTNEWPG6RMZPYEBXTZ8EY`). `signals` has
    a `privacy_class` column, a capability on write and a wall on read;
    `issue_items` and `issue_comments` have none of the three. So a comment
    quoting customer material reaches any caller scoped to the product — and so
    does an item `body` quoting the same thing, which this service has served
    since its first read route.

    Serving the thread widens that surface by one field class inside a vault that
    classifies nothing, rather than opening a boundary that was closed. The
    asymmetry is the issue and it is filed as one; the choice there is between
    classifying the Chart House the way the Archives is classified and recording
    that it is internal-by-construction. Naming it here so the next reader of this
    function does not conclude the absence was considered and approved.
    """
    return [{"id": r["id"], "actor": r["actor"], "kind": r["kind"],
             "body": r["body"], "run_id": r["run_id"], "at": store._iso(r["at"])}
            for r in conn.execute(
                "SELECT id, actor, kind, body, run_id, at FROM issue_comments "
                "WHERE item_id = ? ORDER BY id", (item_id,)).fetchall()]


def _item_history(conn, item_id: str) -> list[dict]:
    """An item's transition log, oldest first — every state change and who made it.

    **THE THIRD WRITE-ONLY LOG, and it is served now because the first two were
    found the expensive way.** `decision_events` (since retired with the
    decision plane) went blank on a page at the write cutover and
    `course_events` broke a stated tool contract; this one is written on every
    open, claim, resolve and link-pr and read back by nothing. All three
    have the same shape, and the shape is why they hide: a client's empty list
    and a field the server never sends look identical.

    **The `note` is what the transition meant.** A resolve carries its outcome
    note, a historical park its reason — the state pair alone says an item moved and not
    why, which for `wontfix` is the entire content of the note.
    """
    total = conn.execute("SELECT COUNT(*) FROM issue_events WHERE item_id = ?",
                         (item_id,)).fetchone()[0]
    rows = conn.execute(
        "SELECT actor, from_state, to_state, note, at FROM issue_events "
        "WHERE item_id = ? ORDER BY id DESC LIMIT ?",
        (item_id, MAX_EVENTS_SERVED)).fetchall()
    events = [{"actor": r["actor"], "from_state": r["from_state"],
               "to_state": r["to_state"], "note": r["note"],
               "at": store._iso(r["at"])}
              for r in reversed(rows)]
    return events, max(0, total - len(events))


@app.get("/items/{item_id}")
async def get_item(item_id: str, request: Request):
    """One work item, its thread and its history — plus, for an epic, a
    `progress` object computed fresh from its children rather than read back
    from a stored count, and a `related_items` list beside it.

    **`reachable` and `reached` are not the same question, and an epic can
    answer them differently.** `reachable` is about the children: every one
    of them has reached `ITEM_TERMINAL`, true vacuously when there are none.
    `reached` is about the epic's own `state`, regardless of what its children
    are doing — an epic can be reached with a child still open (closed early)
    or reachable with the epic itself still open (nobody has closed it out
    yet). A non-epic item carries no `progress` key at all.

    **`related_items` NAMES WHAT `POST /items/{item_id}/epic-relations` WROTE,
    AND IT IS NOT PART OF `progress`.** Those items point at this epic without
    a `parent_id` edge — often from another product, which `_validate_parent`
    refuses `parent_id` from crossing into — so they are not children and do
    not move `progress["children"]` or `reachable`. A non-epic item carries no
    `related_items` key either.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "item.read"):
        return refusals.forbidden("item.read")
    if (err := _reject_unknown_params(request, set())):
        return err
    conn = store.connect()
    try:
        row = conn.execute(
            f"SELECT {store.ITEM_COLUMNS} FROM issue_items WHERE id = ?",
            [item_id]).fetchone()
        # THE SCOPE CHECK AND THE EXISTENCE CHECK ANSWER THE SAME WAY. Splitting
        # them would turn the pair into an existence oracle: an out-of-scope
        # caller could tell a real id from a made-up one by which refusal came
        # back. Checked before the thread is read, so a caller that may not see
        # the item does not cause its comments to be fetched either.
        if row is None or not identity.in_scope(ident, row["product"]):
            return refusals.not_found()
        claim = lease.claim_status(conn, row)
        prerequisites = store.prerequisite_status(
            conn, row, in_scope=_scope_predicate(ident))
        progress = None
        related_items = None
        if row["type"] == store.ITEM_TYPE_EPIC:
            # Reused rather than re-spelled: the same scope predicate `GET
            # /items` applies to its own listing, so a caller never learns a
            # child count for a row it could not list.
            scope, scope_params = _scope_clause(ident)
            progress = store.epic_progress(
                conn, item_id, row["state"],
                scope_clause=scope, scope_params=scope_params)
            # A SEPARATE FIELD FROM `progress`, NEVER MIXED INTO IT. These are
            # items that named this epic through `POST
            # /items/{item_id}/epic-relations` rather than through
            # `parent_id` — `progress["children"]` stays a `parent_id` count,
            # so an epic's reachability still answers the question it always
            # has (issue_01M1JSPH7DBHG0BZFPJPNNZGSC).
            related_items = store.related_items_for_epic(
                conn, item_id, scope_clause=scope, scope_params=scope_params)
        comments = _thread(conn, item_id)
        events, elided = _item_history(conn, item_id)
    finally:
        conn.close()
    # **THE THREAD IS ON THE SINGLE ITEM AND NOT ON THE LIST**, which is a
    # deliberate divergence from Orient rather than a gap (contract §3). Orient
    # inlines every comment on every row, so a 200-row page carries every thread
    # in the queue — the API caps a thread at 200 comments of up to 100,000
    # characters, so that page's size scales with conversation rather than with
    # rows. A reader wanting one item's thread asks for the item.
    result = {**store.item_dict(row, claim=claim, prerequisites=prerequisites),
              "comments": comments,
              "events": events, "events_elided": elided}
    if progress is not None:
        result["progress"] = progress
    if related_items is not None:
        result["related_items"] = related_items
    return result


@app.get("/corrections-queues")
async def list_corrections_queues(request: Request):
    """Every corrections queue the caller's scope admits, or the one named
    by `?product=` (`library/specs/one-home-per-work-item`).

    Scoped exactly as `GET /items` is: `_scope_clause` bounds the query to
    the caller's own products, so a queue outside them is never listed.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "item.read"):
        return refusals.forbidden("item.read")
    if (err := _reject_unknown_params(request, {"product"})):
        return err
    where, params = "WHERE 1 = 1", []
    scope, scope_params = _scope_clause(ident)
    where += scope
    params += scope_params
    if (product := request.query_params.get("product")):
        where += " AND product = ?"
        params.append(product)
    conn = store.connect()
    try:
        rows = conn.execute(
            f"SELECT id, product, created_at FROM corrections_queues "
            f"{where} ORDER BY product", params).fetchall()
    finally:
        conn.close()
    return {"corrections_queues": [store.corrections_queue_dict(r)
                                   for r in rows],
            "count": len(rows)}


@app.get("/corrections-queues/{queue_id}")
async def get_corrections_queue(queue_id: str, request: Request):
    """One product's corrections queue, scoped the same way `GET
    /items/{item_id}` is: an out-of-scope id gets the same 404 as a missing
    one, so neither discloses which it was.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "item.read"):
        return refusals.forbidden("item.read")
    if (err := _reject_unknown_params(request, set())):
        return err
    conn = store.connect()
    try:
        row = store.get_corrections_queue(conn, queue_id)
    finally:
        conn.close()
    if row is None or not identity.in_scope(ident, row["product"]):
        return refusals.not_found()
    return store.corrections_queue_dict(row)


@app.get("/leases")
async def list_leases(request: Request):
    """Who is working inside which subtree, right now — port of
    `convivy-orient/issue_api.py`'s `list_leases` (Keel Phase 2).

    Live leases by default. `?state=all` includes released ones, which is
    where the override rate is read from; `?holder=` narrows to one session.

    A DEAD LEASE IS STILL LISTED, marked `dead: true`. Filtering them out
    would hide exactly the rows an operator is looking for when something is
    stuck — "nothing holds this" and "something abandoned this four hours
    ago" are different answers and the second one is actionable.

    **Scoped through the ROOT ITEM's product**, the join rather than a copy of
    it on the lease — `correct_issue` can move an item between products, and a
    denormalized column would be a second place for product to live and a
    second place for it to go stale.

    **Paginated like every other list**, where Orient's version merely caps at
    a limit with no offset: this is the one divergence from a straight port,
    because every other list route in this file already promises `count` /
    `total` / `truncated` / `offset` and a caller reading this one the same
    way deserves the same contract.

    No `fences` on a listing, matching Orient: enumerating a subtree is a
    walk, and rendering many rows should not each pay for one.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "item.read"):
        return refusals.forbidden("item.read")
    if (err := _reject_unknown_params(
            request, {"state", "limit", "offset", "holder"})):
        return err
    state = request.query_params.get("state") or "live"
    if state not in {"live", "all"}:
        return refusals.refuse(400, "state must be live | all")
    limit, offset, err = _page(request)
    if err:
        return err
    where, params = "WHERE 1 = 1", []
    scope, scope_params = _scope_clause(ident)
    where += scope
    params += scope_params
    if state == "live":
        where += " AND issue_leases.released_at IS NULL"
    if (holder := request.query_params.get("holder")):
        where += " AND issue_leases.holder = ?"
        params.append(holder)
    joined = ("issue_leases JOIN issue_items "
             "ON issue_items.id = issue_leases.root_id")
    conn = store.connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM {joined} {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT issue_leases.* FROM {joined} {where} "
            f"ORDER BY issue_leases.taken_at DESC, issue_leases.id LIMIT ? OFFSET ?",
            [*params, limit, offset]).fetchall()
    finally:
        conn.close()
    return {"leases": [lease.lease_dict(r) for r in rows],
            "policy": lease.policy(),
            "count": len(rows), "total": total, "offset": offset,
            "truncated": offset + len(rows) < total}


@app.get("/lease-refusals")
async def list_lease_refusals(request: Request):
    """Which collisions actually happened — who was refused, by whom, on what.

    **`GET /metrics/librarian` already reports HOW MANY and this reports WHICH.**
    The aggregate answers "is the fence firing too often"; it cannot answer "who
    is colliding with whom", because a rate names nobody. Two crews repeatedly
    blocking each other on the same subtree is invisible in a count and obvious
    in a list, and that collision is the thing somebody has to act on.

    **Serves the fleet-wide and the single-crew view from one route** (the operator,
    2026-08-22), matching `GET /reference/documents`: omit `product` and every
    refusal a caller may see comes back, pass one and it narrows. An operator
    surface wants the fleet; a crew's own cockpit wants its own collisions.

    **Product scoping is the identity's, not the caller's choice.** Unlike the
    Reference Room, refusals name work items, so `_scope_clause` applies exactly
    as it does to every other Chart House read — a `product` parameter narrows
    WITHIN what the identity may already see and can never widen it. A
    single-product token asking for the fleet-wide view gets its own product,
    which is the fleet as far as it is concerned.

    **A refusal names the holder it collided with, and that is disclosure this
    route is deliberately making.** The refused caller already learned that
    holder's name from the 409 that refused it, so nothing new reaches THEM.
    A third reader learns who held what and when, which is one crew's activity
    visible to another — acceptable here because the row is already scoped to
    products the reader can see, and because a collision nobody can see is a
    collision nobody fixes. It is not acceptable everywhere, which is why this
    is a route of its own rather than a field added to a broader listing.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "item.read"):
        return refusals.forbidden("item.read")
    if (err := _reject_unknown_params(request, {
            "limit", "offset", "product", "requester", "blocks_by",
            "blocking_holder"})):
        return err
    limit, offset, err = _page(request)
    if err:
        return err
    scope, params = _scope_clause(ident)
    where = f"WHERE 1=1{scope}"
    if (product := request.query_params.get("product")):
        # NARROWS WITHIN THE SCOPE ABOVE RATHER THAN REPLACING IT. Appended as a
        # second condition, so a caller naming a product it cannot see gets an
        # empty page rather than that product's rows.
        where += " AND product = ?"
        params.append(product)
    for field in ("requester", "blocks_by"):
        if (value := request.query_params.get(field)):
            where += f" AND {field} = ?"
            params.append(value)
    # **FILTERING ON A HOLDER MUST NOT MATCH A ROW WHOSE HOLDER IS WITHHELD**
    # (security-reviewer, PR #48 round 2). Withholding `blocking_holder` from
    # the BODY is not enough while the same value can be filtered on: a caller
    # guesses a holder, reads `total`, and learns whether that holder collided
    # with its scope. Crew and service identities are a small enumerable set, so
    # a boolean count is a full oracle for exactly the fact the withholding
    # exists to keep.
    #
    # So the filter is correlated with the same visibility the body uses: it can
    # only match a row whose blocking subtree the caller may already see. A guess
    # against an invisible row now returns nothing, which is indistinguishable
    # from a wrong guess — which is the property being restored.
    #
    # `requester` and `blocks_by` need no equivalent. `requester` is the caller's
    # own side of the refusal, and `blocks_by` is a direction with two values.
    if (holder := request.query_params.get("blocking_holder")):
        holder_scope, holder_params = _scope_clause(ident)
        where += (" AND blocking_holder = ? AND blocking_root_id IN "
                  f"(SELECT id FROM issue_items WHERE 1 = 1{holder_scope})")
        params.append(holder)
        params.extend(holder_params)
    conn = store.connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM lease_refusals {where}", params).fetchone()[0]
        rows = conn.execute(
            # NEWEST FIRST, with `id` breaking ties — `at` is a second-resolution
            # timestamp and two refusals inside one second are exactly what a
            # collision storm looks like, so ordering on it alone would page
            # unstably at the moment the data matters most.
            f"SELECT * FROM lease_refusals {where} "
            "ORDER BY at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset]).fetchall()
    finally:
        conn.close()
    # **THE BLOCKING LEASE'S OWN PRODUCT IS A SEPARATE FACT FROM THE REQUESTED
    # ITEM'S** (security-reviewer, PR #48). `lease.record_refusal` stamps
    # `product` from the item that was REFUSED, so the scope clause above proves
    # only that the caller may see that item. The lease that blocked it sits on
    # a different row, and `blocking_holder` names its holder.
    #
    # Every other site in this file that discloses a blocking holder calls
    # `_lease_root_visible` for exactly this reason, and that helper's docstring
    # records that two of the three forgot it first. Unreachable today, because
    # `_validate_parent` makes every parent edge same-product — but the check
    # belongs at the disclosure rather than in an argument about an invariant
    # somewhere else, which is the lesson that helper exists to carry.
    #
    # BATCHED, not one lookup per row: a refusal listing is a page, and an N+1
    # here would be the same shape the Course listing already avoids.
    conn = store.connect()
    try:
        roots = {r["blocking_root_id"] for r in rows if r["blocking_root_id"]}
        visible = set()
        if roots:
            placeholders = ",".join("?" * len(roots))
            scope2, scope2_params = _scope_clause(ident)
            for r in conn.execute(
                    f"SELECT id FROM issue_items WHERE id IN ({placeholders})"
                    f"{scope2}", [*roots, *scope2_params]):
                visible.add(r["id"])
    finally:
        conn.close()
    return {"refusals": [lease.refusal_dict(
                r, blocker_visible=r["blocking_root_id"] in visible)
            for r in rows],
            "count": len(rows), "total": total, "offset": offset,
            "truncated": offset + len(rows) < total}


@app.get("/metrics/librarian")
async def librarian_metrics_route(request: Request):
    """ADR-0019's instrumentation: the numbers that decide whether the
    librarian ever needs a model. Port of
    `convivy-orient/issue_api.py`'s `librarian_metrics_route`.

    Four measurements, each carrying its own `available` flag. Two are real —
    the duplicate-filing rate and the lease refusals ADR-0019 asks to be read
    beside it. Two report `available: false` with the evidence, because a
    trigger that silently reads zero because nothing feeds it is the owner
    problem ADR-0019 exists to prevent, wearing a different costume.

    `?window_days=` and `?product=` narrow it. Read-only.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "item.read"):
        return refusals.forbidden("item.read")
    if (err := _reject_unknown_params(request, {"window_days", "product"})):
        return err
    try:
        window = int(request.query_params.get(
            "window_days", librarian_metrics.DEFAULT_WINDOW_DAYS))
    except (TypeError, ValueError):
        return refusals.refuse(400, "window_days must be an integer")
    if not 1 <= window <= 3650:
        return refusals.refuse(400, "window_days must be between 1 and 3650")
    product = request.query_params.get("product")
    # SCOPE IT, because these are counts over other products' work items. An
    # unscoped total would tell a single-product token how much work exists
    # elsewhere.
    if product and not identity.in_scope(ident, product):
        return refusals.not_found()
    if not product and "*" not in ident.products:
        return refusals.refuse(
            400, "product is required for a scoped identity",
            hint="an unscoped total would count work items outside your scope")
    conn = store.connect()
    try:
        snap = librarian_metrics.snapshot(conn, window_days=window,
                                          product=product)
    finally:
        conn.close()
    return snap


@app.get("/courses")
async def list_courses(request: Request):
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "course.read"):
        return refusals.forbidden("course.read")
    if (err := _reject_unknown_params(
            request, {"limit", "offset", "status", "product"})):
        return err
    # PAGINATED LIKE EVERY OTHER LIST, and it was not (reviewer, PR #1). The
    # contract says every list response carries `count`, `total`, `truncated` and
    # `offset`, and a client reads a MISSING `total` as "this page may be
    # incomplete" — the right signal from an older server and a wrong one from the
    # first one. There are two Courses today, which is exactly why the omission
    # looked harmless and why it had to be fixed before anyone wrote a client.
    limit, offset, err = _page(request)
    if err:
        return err
    where, params = "WHERE 1 = 1", []
    scope, scope_params = _scope_clause(ident)
    where += scope
    params += scope_params
    # `live` by default. A superseded Course is what a product USED to intend, and
    # returning it unasked would put a retired plan beside the current one with
    # nothing but a status field to tell them apart.
    status = request.query_params.get("status", store.COURSE_LIVE)
    if status != "all":
        where += " AND status = ?"
        params.append(status)
    if (product := request.query_params.get("product")):
        # RESOLVED THROUGH THE PROJECT REGISTRY, matching Orient — a literal
        # match here is why an alias or a non-canonical casing silently
        # returned nothing where Orient returned the Course (reviewer, PR #23).
        where += " AND product = ?"
        params.append(projects.resolve(product) or product)
    conn = store.connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM courses {where}", params).fetchone()[0]
        rows = conn.execute(
            # ORDERED BY `created_at`, MATCHING ORIENT, and the shadow harness is
            # what found it: this listed by `updated_at DESC` and returned the
            # two live Courses in the opposite order to Orient for every
            # wildcard identity. `updated_at` is also nullable — a Course nobody
            # has revised has none — so ordering on it put an unrevised Course
            # wherever SQLite happens to sort NULL, which is not an order anyone
            # chose.
            f"SELECT * FROM courses {where} ORDER BY created_at DESC, id "
            f"LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
    finally:
        conn.close()
    return {"courses": [store.course_dict(r) for r in rows],
            "count": len(rows), "total": total, "offset": offset,
            "truncated": offset + len(rows) < total}


# **A TRANSITION LOG IS BOUNDED AT READ TIME, not at write time, and the
# difference is the whole reason this constant is not `MAX_COMMENTS_PER_ITEM`.**
# A comment can be refused — the write path caps a thread at 200 and the caller
# simply does not get to add the 201st. A transition cannot: it already happened,
# and a log that declined to record it would be an audit trail that lies. So the
# bound lives on the read, and the count of what it left out travels with it.
#
# **The unbounded case is real rather than theoretical** (security-reviewer,
# convivy/library#36). A course event carries a full copy of the plan — up to 500
# ids — and a reorder is gated only by an optimistic-concurrency version a caller
# can always supply correctly. A Course reordered weekly for a year is fifty
# snapshots inlined into one GET.
#
# The most RECENT are kept, matching what `decisions_build` already renders
# ("N earlier transitions not shown"): the question a history answers is how the
# thing got to where it is now, and that is answered from the recent end.
MAX_EVENTS_SERVED = 50

def _course_history(conn, course_id: str) -> list[dict]:
    """A Course's change log, oldest first — how the plan's order got this way.

    **THE NOTE IS THE POINT.** A reorder without its reasoning is a record that
    something moved; the note is why, and it is the only place that survives.
    "agentctl's model lane slots after the issue/epic/Course machinery, because
    it cannot attribute cost to planned work until a Course reports progress" is
    an argument nobody reconstructs from two orderings.

    **`entries` travels with the event** because a reorder's subject IS the order
    it produced. An event carrying only an action tells a reader the plan changed
    and not what it changed to, which is the same half-record this route already
    served for decisions until `issue_01KZVKAVTGE0CS0XAC42B9A8JE`.

    The `ops` MCP's own `course_get` contract promises "the history of how the
    order has changed", so a Library that served the plan without it broke a
    stated contract rather than merely omitting a field.
    """
    total = conn.execute("SELECT COUNT(*) FROM course_events WHERE course_id = ?",
                         (course_id,)).fetchone()[0]
    # The newest `MAX_EVENTS_SERVED`, handed back oldest-first so a reader walks
    # the story forwards even when the window starts mid-way.
    rows = conn.execute(
        "SELECT actor, action, entries, note, at FROM course_events "
        "WHERE course_id = ? ORDER BY id DESC LIMIT ?",
        (course_id, MAX_EVENTS_SERVED)).fetchall()
    events = [{"actor": r["actor"], "action": r["action"],
               "entries": store._json_or(r["entries"], []),
               "note": r["note"], "at": store._iso(r["at"])}
              for r in reversed(rows)]
    return events, max(0, total - len(events))


@app.get("/courses/{course_id}")
async def get_course(course_id: str, request: Request):
    """A Course, its resolved items, its history, and its `position`.

    **`position` REPORTS WHICH ENTRIES ARE REACHED, IN FLIGHT, AND
    REMAINING — NOTHING ELSE.** An entry is REACHED when its own state is a
    terminal resolution (`store.ITEM_TERMINAL`); an epic with zero children
    can be complete, so this never looks at descendants. An entry is IN
    FLIGHT when it is not reached and either its own state is not `open`
    (claimed or pr-open, regardless of whether that claim has since
    expired), or an unreleased, non-dead lease is rooted on it — the signal
    a checked-out child beneath an open epic, or a blocked epic's own
    checkout (platform/decisions/0079), leaves behind with the epic's own
    row still `open`. Several entries may be in flight at once; it is a
    list, never a single current or next entry. Everything else open is
    REMAINING. A missing entry (its epic gone or out of scope) lands in
    none of the three lists and is counted separately.

    **THE COURSE'S ORDER IS A PLAN, NOT A SCHEDULE.** `position` never
    names a next entry or ranks what can start — no `next`, `current`,
    `up_next` or `eta` — because nothing here decides that a Course entry
    can start only once the one before it is reached.

    **`waypoint_progress` AND `position["synthetic_waypoints"]` ARE THE
    SAME PAIR, FOR THE COURSE'S SYNTHETIC WAYPOINTS** (`courses.waypoints` —
    "epic" and "waypoint" are defined once, in `lease.py`'s module
    docstring). `waypoint_progress` is the detail, one entry per synthetic
    waypoint with its members' own ids and states — the counterpart to
    `items` above. `position["synthetic_waypoints"]` places each synthetic
    waypoint into reached, in flight or remaining, the counterpart to
    `position`'s own epic-entry fields, which stay exactly as they were:
    neither key existed before this, and nothing above them changed shape
    or meaning to make room for them.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "course.read"):
        return refusals.forbidden("course.read")
    if (err := _reject_unknown_params(request, set())):
        return err
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT * FROM courses WHERE id = ?", [course_id]).fetchone()
        if row is None or not identity.in_scope(ident, row["product"]):
            return refusals.not_found()
        course = store.course_dict(row)
        entries = course["entries"]
        waypoints = course["waypoints"]
        items = {}
        if entries:
            # SCOPED LIKE EVERY OTHER FETCH, and it was not (reviewer, PR #1).
            # Course entries are ids read out of a JSON column, so nothing
            # enforces that an entry names an item in the Course's own product. A
            # cross-product entry is a data-integrity fault rather than a normal
            # state — and resolving one here would hand its full body to a caller
            # scoped only to the Course, which is the wrong direction to fail.
            #
            # An entry filtered out this way reads as `missing` below, which is
            # the honest answer: the caller cannot see it, and saying so beats
            # both silently dropping it and disclosing it.
            placeholders = ",".join("?" * len(entries))
            scope, scope_params = _scope_clause(ident)
            for r in conn.execute(
                    f"SELECT {store.ITEM_COLUMNS} FROM issue_items "
                    f"WHERE id IN ({placeholders}){scope}",
                    [*entries, *scope_params]):
                items[r["id"]] = r
        # BATCHED, like the item listing's — a Course resolves to a page of items
        # and one lease lookup per claimed entry is the same N+1.
        claims = lease.claim_statuses(conn, list(items.values()))
        prereqs = store.prerequisite_statuses(conn, list(items.values()),
                                              in_scope=_scope_predicate(ident))
        # BATCHED THE SAME WAY, over the entry ids directly rather than the
        # resolved rows: an entry is always an epic (`_validate_entries`), and an
        # epic leases itself, so its own id IS the lease root `position` needs —
        # no walk and no per-entry query. `claims` above cannot stand in for this:
        # it only ever answers for a row already in state `claimed`, and the case
        # `position` exists to catch is exactly the one where the epic's own row
        # stays `open` while a lease still fences it.
        entry_leases = lease.live_lease_rows(conn, entries) if entries else {}
        # A SEPARATE SCOPED QUERY, over the union of every synthetic waypoint's
        # own named members — a member set is not the entries list, and it may
        # name a work item `_validate_entries` would refuse, so `items` above
        # cannot be reused for it. No lease lookup here: `waypoint_course_
        # position`'s own docstring says why a member's own state is all it
        # looks at.
        member_ids = sorted({mid for wp in waypoints for mid in wp["members"]})
        waypoint_items = {}
        if member_ids:
            placeholders = ",".join("?" * len(member_ids))
            scope, scope_params = _scope_clause(ident)
            for r in conn.execute(
                    f"SELECT {store.ITEM_COLUMNS} FROM issue_items "
                    f"WHERE id IN ({placeholders}){scope}",
                    [*member_ids, *scope_params]):
                waypoint_items[r["id"]] = r
        events, elided = _course_history(conn, course_id)
    finally:
        conn.close()
    # AN ENTRY WHOSE EPIC IS GONE IS REPORTED, NOT DROPPED. A silently shorter
    # list reads as a plan with fewer steps; `missing` says the plan points at
    # something that no longer exists, which is a thing somebody has to fix.
    course["items"] = [
        (store.item_dict(items[eid], claim=claims.get(eid, (False, None)),
                         prerequisites=prereqs[eid])
         if eid in items else {"id": eid, "missing": True})
        for eid in entries]
    course["events"] = events
    course["events_elided"] = elided
    waypoint_progress = [
        {"id": wp["id"],
         **store.waypoint_progress(wp["members"], waypoint_items)}
        for wp in waypoints]
    course["waypoint_progress"] = waypoint_progress
    course["position"] = lease.course_position(entries, items, entry_leases)
    course["position"]["synthetic_waypoints"] = (
        lease.waypoint_course_position(waypoint_progress))
    return course


def _validate_entries(conn, ident, product: str, raw):
    """A Course's order against the membership rules, or a `(status, body)`
    refusal.

    In one function because two routes write the order, and a rule enforced on
    create but not on reorder is a rule the second write quietly undoes.

    **A COURSE CONTAINS ONLY EPICS** (the operator, 2026-08-05), and the reason reaches
    past storage. A correction is a break FROM Course work and is drained by
    autonomous crews, so the Course is the planned lane and the corrections
    queue is the interrupt lane; accepting a work item that is not an epic here
    would merge the two without anybody deciding to.

    Everything else it checks is a way the order could stop meaning anything: an
    id naming nothing, an epic belonging to another project, or the same epic
    twice, which has no defined position and makes the array disagree with what
    a reader sees.

    **THE LOOKUP IS SCOPED, WHERE ORIENT'S IS NOT, and the divergence is
    deliberate.** Orient answers "no such work item" for an unknown id and
    "wrong product" for a real one belonging elsewhere, which is an existence
    oracle of exactly the shape `_validate_parent` refuses to be: a caller
    holding one product's token could submit ids one at a time and learn which
    of them exist in products it cannot see. An entry outside this identity's
    scope reads as unknown here, in the same wording an invented id gets. Past
    that point the caller can already read the row, so naming its product
    discloses nothing it could not fetch directly.

    Every refusal names ALL the offending ids rather than the first, because a
    caller assembling a plan would otherwise fix them one round trip at a time.
    """
    # **NO SILENT None-TO-EMPTY CONVERSION HERE, and that used to be a real hole**
    # (reviewer, PR #15). This helper is shared by a route where `entries` is
    # optional and a route where it is required, so a convenience that turns a
    # missing array into an empty one belongs at the OPTIONAL caller's site
    # rather than in the shared code. It was here, and the reorder route's
    # required-field check tested `"entries" not in payload` — so `entries: null`
    # arrived as a present key holding None, passed the check, was converted to
    # `[]` down here, and emptied the plan with a 200. That is precisely the lost
    # update the required-field rule exists to refuse, walking through the rule.
    if not isinstance(raw, list):
        return None, (400, {"error": "entries must be an array of epic ids"})
    if len(raw) > store.MAX_COURSE_ENTRIES:
        return None, (400, {
            "error": f"entries may hold at most {store.MAX_COURSE_ENTRIES} epics",
            "hint": "a plan this long is a symptom rather than a route; consider "
                    "nesting epics"})
    entries = []
    for e in raw:
        if not isinstance(e, str) or not e.strip():
            return None, (400, {
                "error": "every entry must be a non-empty epic id"})
        entries.append(e.strip())
    dupes = sorted({e for e in entries if entries.count(e) > 1})
    if dupes:
        return None, (400, {"error": "an epic may appear in a Course only once",
                            "duplicates": dupes})
    if not entries:
        return entries, None
    scope, scope_params = _scope_clause(ident)
    rows = {r["id"]: r for r in conn.execute(
        f"SELECT id, product, type FROM issue_items "
        f"WHERE id IN ({','.join('?' * len(entries))}){scope}",
        [*entries, *scope_params])}
    unknown = sorted(e for e in entries if e not in rows)
    if unknown:
        return None, (400, {
            "error": "no such work item, or none this identity may see",
            "unknown": unknown})
    not_epics = sorted(e for e in entries
                       if rows[e]["type"] != store.ITEM_TYPE_EPIC)
    if not_epics:
        return None, (400, {
            "error": "a Course contains only epics",
            "not_epics": not_epics,
            "hint": "a work item that is not an epic belongs under an epic or "
                    "in its product's corrections queue; put the work item "
                    "under an epic and put the epic on the Course"})
    foreign = sorted(e for e in entries if rows[e]["product"] != product)
    if foreign:
        return None, (400, {
            "error": f"every epic must belong to product {product!r}",
            "wrong_product": foreign})
    return entries, None


def _load_course(conn, ident, course_id: str):
    """A Course inside the caller's product scope, or None.

    Out of scope is indistinguishable from absent, the same shape every other
    fetch in this file takes: a 404 must never confirm that a product exists to
    someone who cannot see it.
    """
    row = conn.execute("SELECT * FROM courses WHERE id = ?",
                       (course_id,)).fetchone()
    if row is None or not identity.in_scope(ident, row["product"]):
        return None
    return row


def _course_writer(request: Request):
    """`(identity, None)` for a caller that may write Courses, or `(None,
    refusal)`.

    **BOTH `course.write` AND `course.read`, because every Course write route
    answers with a Course** (security-reviewer, PR #15). Each of them returns a
    full `course_dict` on success, and the reorder route's version-mismatch 409
    returns `current_entries` — the whole ordered list of epic ids — because a
    caller told only "you are stale" cannot rebase. That is read-shaped data
    coming out of a write endpoint.

    Today the pairing is inert: both roles in `ROLE_CAPS` carry the two
    capabilities together, so anyone reaching these bodies could fetch the same
    values from `GET /courses/{id}`. Nothing enforces that, though. It is
    incidental to a two-row table, and a narrower role granted `course.write`
    alone — a reorder-only automation is the obvious one — would find these the
    single path handing it the order it was deliberately not granted read access
    to. Making the coupling a check rather than an accident means such a role
    trips a visible 403 instead of silently reopening the disclosure.

    In one function so the rule cannot be applied to three routes and forgotten
    on the fourth, which is the failure this file has already had twice.
    """
    ident, err = _identify(request)
    if err:
        return None, err
    for capability in ("course.write", "course.read"):
        if not identity.authorized(ident, capability):
            return None, refusals.forbidden(capability)
    return ident, None


def _course_not_found() -> tuple[int, dict]:
    """One wording for every Course 404, for the reason `refusals.not_found`
    exists: absent and out-of-scope must be one answer, and three routes each
    writing their own version of it is how the two drift apart into an oracle.

    **A FUNCTION RATHER THAN A SHARED CONSTANT** (reviewer, PR #14). A
    module-level tuple wrapping one dict is returned by reference from every
    404, so a future call site adding a `hint` would corrupt the body for every
    later request in the process. `refusals.refuse` builds a fresh dict per call
    and so does this; the hazard costs one function call to remove.
    """
    return 404, {"error": "no such Course, or none this identity may see"}


def _frozen_course(course) -> tuple[int, dict] | None:
    """A superseded Course refuses every write, or None if it is still live.

    **SHARED, BECAUSE A GUARD ON SOME WRITE ROUTES AND NOT OTHERS IS HOW FROZEN
    STOPS BEING TRUE.** Orient learned this on its own PR #161, where `retitle`
    lacked the check that `set_order` had and a retired plan stayed silently
    renameable. A superseded Course is the record of a route we no longer intend
    to take, so editing it rewrites history rather than changing a plan.
    """
    if course["status"] == store.COURSE_LIVE:
        return None
    return 409, {
        "error": f"this Course is {course['status']} and is kept as a record; "
                 f"it cannot be changed",
        "hint": "create a new Course for the route you mean to take now"}


def _live_course_refusal(conn, product: str) -> tuple[int, dict] | None:
    """The one-live-Course-per-product conflict, naming the Course that holds the
    slot. Shared between the pre-check and the lost-race path below, so the two
    cannot drift into telling a caller different things about the same state."""
    live = conn.execute(
        "SELECT id FROM courses WHERE product = ? AND status = ?",
        (product, store.COURSE_LIVE)).fetchone()
    if live is None:
        return None
    return 409, {
        "error": f"{product} already has a live Course",
        "course_id": live["id"],
        "hint": "reorder it, or supersede it first if this is a new route"}


@app.post("/courses")
async def create_course(request: Request):
    """Start a plan for a project.

    **At most one live Course per product**, enforced by the partial unique index
    on `courses`. This checks first anyway, so a caller gets the id of the plan
    already holding the slot rather than a bare integrity error it cannot act on.

    `product` is resolved against the Keel's project registry and the canonical
    slug is stored, for the reason `POST /items` gives at length: a Course filed
    under a variant spelling is a plan nobody looking at that project will find.
    Scope is checked against the RESOLVED slug, so a token scoped to `keel` is
    neither refused for writing `Substrate` nor admitted for writing something
    that merely looks like its product.
    """
    ident, err = _course_writer(request)
    if err:
        return err
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    for field in ("product", "title"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            return refusals.refuse(400, f"{field} is required")
    # **THE LENGTH IS CHECKED ON THE VALUE THAT WILL BE STORED, which is the
    # trimmed one** (reviewer, PR #13). Checking the raw string while storing the
    # trimmed one refused a title that fits: 300 characters behind three spaces
    # is 303 raw and exactly `MAX_TITLE` once stripped, and the caller was told it
    # was too long about a value nobody would ever have written. `POST /items`
    # avoids this by checking and clipping the SAME string; this route trims, so
    # the check has to trim with it.
    title = payload["title"].strip()
    if len(title) > store.MAX_TITLE:
        return refusals.refuse(
            400, f"title is longer than {store.MAX_TITLE} characters")
    if len(payload["product"]) > store.MAX_IDENT:
        return refusals.refuse(
            400, f"product is longer than {store.MAX_IDENT} characters")

    product = projects.resolve(payload["product"])
    if product is None:
        # NARROWED TO WHAT THIS CALLER MAY FILE INTO, the same way `POST /items`
        # narrows it: returning every registered name turns a slow oracle into a
        # directory dump, and telling a `vivy`-scoped agent that `constance`
        # exists does not help it file correctly either.
        known = [n for n in projects.known_names()
                 if identity.in_scope(ident, projects.resolve(n))]
        return refusals.refuse(
            400, f"no project or scope named {payload['product']!r}",
            hint="a slug, alias, repo name or non-project scope resolves; the "
                 "canonical slug is what gets stored",
            known=known)
    if not identity.in_scope(ident, product):
        return refusals.not_found()

    try:
        status, body = await run_in_threadpool(
            _do_create_course, ident, product,
            # `clip` is redundant against the check above and kept anyway, as the
            # write-side half of the bound rather than the caller-facing one.
            # SQLite does not enforce `VARCHAR(300)`, so the column has no
            # defence of its own if the check and the write ever drift apart.
            store.clip(title, store.MAX_TITLE),
            payload.get("entries"))
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    return JSONResponse(status_code=status, content=body)


def _do_create_course(ident, product: str, title: str, raw_entries):
    conn = store.connect_rw()
    try:
        # **THE WRITE LOCK IS TAKEN BEFORE THE CHECK, not before the insert**,
        # and it is the check-then-act that needs it. "At most one live Course
        # per product" is read with a SELECT, and a SELECT takes no lock, so two
        # concurrent creates could both read no live Course and both proceed.
        # The index would then reject one of them with an integrity error, where
        # a 409 naming the winning Course was the answer its caller needed.
        # `lease.take` holds the lock for the same reason.
        #
        # UNGUARDED, where `lease.take` writes `if not conn.in_transaction`, and
        # the difference is real rather than an inconsistency (reviewer, PR #13).
        # `lease.take` is handed a connection by its caller and cannot know what
        # state it arrives in. This function opens its own through `connect_rw`,
        # which returns a fresh unpooled connection every time, so the guard here
        # would be a branch that never runs — reading as though a pre-transacted
        # connection were a possibility.
        conn.execute("BEGIN IMMEDIATE")
        conflict = _live_course_refusal(conn, product)
        if conflict:
            return conflict
        # **CREATE IS THE ROUTE WHERE `entries` IS OPTIONAL, so the default lives
        # here rather than in the shared validator.** Absent means "start the
        # plan empty and fill it in later", which is a real and common way to
        # file one. The reorder route requires the field instead, and putting
        # this convenience in `_validate_entries` was how a `null` reached that
        # route's write as an empty array (reviewer, PR #15).
        entries, err = _validate_entries(
            conn, ident, product, [] if raw_entries is None else raw_entries)
        if err:
            return err
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        course_id = f"course_{store.new_ulid()}"
        try:
            conn.execute(
                "INSERT INTO courses (id, product, title, entries, status, "
                "version, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (course_id, product, title, json.dumps(entries),
                 store.COURSE_LIVE, 1, ident.actor, now.isoformat(" ")))
            conn.execute(
                "INSERT INTO course_events (course_id, actor, action, entries, at) "
                "VALUES (?,?,?,?,?)",
                # The snapshot is written only when the order is non-empty, so a
                # NULL keeps meaning "the order did not move here" rather than
                # also meaning "it moved to nothing".
                #
                # **THE REORDER ROUTE MUST NOT REUSE THIS TERNARY** (reviewer,
                # PR #13). It is right here because a brand-new empty Course
                # never moved anywhere. A reorder always represents a move,
                # including a move TO empty, so the same expression there would
                # write NULL for "reordered to nothing" and make it
                # indistinguishable from "nothing happened" — losing the one
                # event that explains why a plan is empty.
                (course_id, ident.actor, store.COURSE_ACTION_CREATED,
                 json.dumps(entries) if entries else None, now.isoformat(" ")))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            # LOST THE INSERT RACE, which the lock above should have made
            # unreachable. It stays handled because the index is the real
            # enforcement and this is the answer that names it. A conflict with
            # no live Course behind it is some other constraint failing — an id
            # clash, effectively unreachable at ULID odds — and reporting that as
            # a duplicate plan would send the caller looking for one that is not
            # there. `insert_item` falls through to the generic message in the
            # same situation and so does this.
            return _live_course_refusal(conn, product) or (409, {"error": "conflict"})
        row = conn.execute("SELECT * FROM courses WHERE id = ?",
                           (course_id,)).fetchone()
        return 201, store.course_dict(row)
    finally:
        conn.close()


def _validate_course_note(payload: dict):
    """The optional `note` on a Course write, or a `(status, body)` refusal.

    **THE TWO FAILURES GET DIFFERENT MESSAGES.** Collapsing "not a string" and
    "longer than the cap" into one wording leaves a caller holding a perfectly
    good note unable to tell whether to retype it or shorten it (Orient's
    reviewer, its PR #161).
    """
    note = payload.get("note")
    if note is None:
        return None, None
    if not isinstance(note, str):
        return None, (400, {"error": "note must be a string"})
    if len(note) > store.MAX_COURSE_NOTE:
        return None, (400, {
            "error": f"note is longer than {store.MAX_COURSE_NOTE} characters",
            "length": len(note)})
    return note, None


def _validate_successor_field(payload: dict):
    """The optional `successor` on a supersede: `{"title", "raw_entries"}`, or a
    `(status, body)` refusal, or `(None, None)` when no hand-off was asked for.

    **Shape only.** `title` is checked the same way `POST /courses` checks it —
    trimmed before the length check, for the reason that route gives: the check
    and the stored value have to agree on which string they mean. `entries` is
    checked here only for being an array; the ids it names are epics of a
    specific product only `_validate_entries` can confirm, and that needs a
    connection this function does not have. It is validated inside the
    transaction, alongside the stranding check the hand-off exists for.
    """
    raw = payload.get("successor")
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, (400, {"error": "successor must be an object"})
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return None, (400, {"error": "successor.title is required"})
    title = title.strip()
    if len(title) > store.MAX_TITLE:
        return None, (400, {
            "error": f"successor.title is longer than {store.MAX_TITLE} "
                     f"characters"})
    entries = raw.get("entries")
    if not isinstance(entries, list):
        return None, (400, {
            "error": "successor.entries must be an array of epic ids"})
    return {"title": title, "raw_entries": entries}, None


@app.put("/courses/{course_id}/order")
async def set_course_order(request: Request, course_id: str):
    """Replace the order. THE reorder path, and one write.

    **`version` is required and checked, and this is the only route in the
    Library with real concurrency semantics.** Setting the order is a whole-array
    replacement, so two callers that both read version N and both write produce a
    silent lost update: the second write is complete and correct on its own terms
    and simply does not contain the first one's change. A rank column would have
    merged them by accident; an array cannot. The mismatch comes back as a 409
    carrying the current order, so the caller can rebase its edit rather than
    re-fetch and guess.

    **`entries` IS REQUIRED HERE, WHERE ORIENT TREATS AN ABSENT ARRAY AS AN EMPTY
    ONE, and that is a divergence rather than an oversight.** Under Orient's
    reading a client bug that drops the field sends `{"version": 3}` and empties
    the plan, which is the largest lost update this route can produce, arriving
    through the one door built to prevent lost updates. Clearing a Course is
    still available and still one request: send `entries: []` and mean it. This
    route has no v1 behaviour to tighten, so requiring the field at birth is
    additive rather than a contract break.
    """
    ident, err = _course_writer(request)
    if err:
        return err
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    version = payload.get("version")
    # `bool` IS A SUBCLASS OF `int`, so `True` would otherwise read as version 1
    # and pass against a fresh Course. A caller sending a boolean has a bug, and
    # the one place it must not be quietly interpreted is the check that exists
    # to catch a stale read.
    if not isinstance(version, int) or isinstance(version, bool):
        return refusals.refuse(
            400, "version is required and must be an integer",
            hint="read the Course first and send back the version you saw — it "
                 "is what stops two concurrent reorders from silently "
                 "discarding one of them")
    # `.get(...) is None` RATHER THAN `not in`, and the difference is the whole
    # control (reviewer, PR #15). A dropped field arrives over JSON as an absent
    # key OR as an explicit `null` depending on how the client serialises, and
    # `"entries" not in payload` catches only the first. The second passed, and
    # then emptied the plan.
    if payload.get("entries") is None:
        return refusals.refuse(
            400, "entries is required",
            hint="send the full order you want, or an empty array to clear the "
                 "plan; a missing or null array would make a dropped field empty "
                 "a Course silently")
    note, err = _validate_course_note(payload)
    if err:
        return JSONResponse(status_code=err[0], content=err[1])
    try:
        status, body = await run_in_threadpool(
            _do_set_course_order, ident, course_id, version,
            payload["entries"], note)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    return JSONResponse(status_code=status, content=body)


def _do_set_course_order(ident, course_id: str, version: int, raw_entries, note):
    conn = store.connect_rw()
    try:
        # THE WRITE LOCK BEFORE THE READ, for the reason `_do_create_course`
        # gives and with more at stake here: the version check IS a check-then-act
        # and a SELECT takes no lock, so two callers holding the same version
        # could both read it, both find it current, and both write. That is
        # precisely the lost update the version exists to prevent, arriving
        # through the check meant to prevent it. Unguarded for the reason
        # `_do_create_course` gives: `connect_rw` hands back a fresh unpooled
        # connection, so `if not conn.in_transaction` would never run.
        conn.execute("BEGIN IMMEDIATE")
        course = _load_course(conn, ident, course_id)
        if course is None:
            return _course_not_found()
        frozen = _frozen_course(course)
        if frozen:
            return frozen
        if version != course["version"]:
            return 409, {
                "error": "version mismatch: this Course changed since you read it",
                "your_version": version,
                "current_version": course["version"],
                "current_entries": store.course_dict(course)["entries"],
                "hint": "rebase your change onto current_entries and retry"}
        entries, err = _validate_entries(conn, ident, course["product"],
                                         raw_entries)
        if err:
            return err
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        # **THE UPDATE IS CONDITIONAL ON BOTH THE VERSION AND THE STATUS, and the
        # rowcount is checked.** The lock above makes the read-then-write safe;
        # this makes it safe without depending on the lock, which is the same
        # belt-and-braces shape `_do_create_course` gets from the partial unique
        # index. A compare-and-swap in one statement cannot be split by anything,
        # so a future refactor that moves or drops the `BEGIN IMMEDIATE` degrades
        # to a 409 rather than to a silent lost update.
        #
        # **THE STATUS HALF IS WHAT MAKES THAT SENTENCE TRUE, and guarding the
        # version alone did not** (reviewer, PR #17). Every other route's second
        # control matches the race its own route is exposed to. Reorder's race is
        # a concurrent SUPERSEDE, which does not touch `version` at all — so
        # without the lock, a reorder that read `status = live` could commit its
        # entries onto a Course another caller had just frozen, which is the
        # silent lost update this control exists to rule out. Invisible while
        # reviewing this route alone, because until the supersede route landed no
        # write path could move a Course off `live`. `IS ?` rather than `= ?`, so
        # a NULL status compares rather than matching nothing, the same as the
        # other two status swaps.
        cur = conn.execute(
            "UPDATE courses SET entries = ?, version = version + 1, "
            "updated_by = ?, updated_at = ? "
            "WHERE id = ? AND version = ? AND status IS ?",
            (json.dumps(entries), ident.actor, now.isoformat(" "), course_id,
             version, course["status"]))
        if cur.rowcount != 1:
            conn.rollback()
            # **RE-READ THROUGH `_load_course`, SO THE SCOPE CHECK IS NOT SKIPPED
            # ON THE WAY OUT** (reviewer, PR #15). A bare SELECT by id here is
            # not reachable with a row this caller may not see — `courses.product`
            # is written once at create and by no route since — but the refusal
            # it builds carries `current_entries`, the Course's full ordered list
            # of epic ids. A disclosure that is safe only because of an invariant
            # enforced somewhere else is the shape this file keeps finding bugs
            # in, and one function call removes the argument entirely.
            fresh = _load_course(conn, ident, course_id)
            if fresh is None:
                return _course_not_found()
            # THE FROZEN CASE ANSWERS FIRST, matching retitle's fallback
            # (reviewer, PR #17). The CAS now fails for two reasons and this
            # branch reported only one of them, so a reorder losing to a
            # supersede was told its version was stale — true in no useful sense,
            # and it would send the caller off to rebase onto a plan that can
            # never accept the write.
            return _frozen_course(fresh) or (409, {
                "error": "version mismatch: this Course changed since you read it",
                "your_version": version,
                "current_version": fresh["version"],
                "current_entries": store.course_dict(fresh)["entries"],
                "hint": "rebase your change onto current_entries and retry"})
        conn.execute(
            "INSERT INTO course_events (course_id, actor, action, entries, note, at) "
            "VALUES (?,?,?,?,?,?)",
            # **`reordered` IS THE ONLY ACTION THAT CARRIES A SNAPSHOT, and it
            # carries one even when the new order is empty.** Create writes NULL
            # for an empty array because "the order did not move here" is the
            # fact it is recording; here the order DID move, and to nothing, so a
            # NULL would lose the one event that explains why the plan is empty.
            (course_id, ident.actor, store.COURSE_ACTION_REORDERED,
             json.dumps(entries), note, now.isoformat(" ")))
        conn.commit()
        fresh = _load_course(conn, ident, course_id)
        return 200, store.course_dict(fresh)
    finally:
        conn.close()


@app.patch("/courses/{course_id}")
async def retitle_course(request: Request, course_id: str):
    """Rename a plan.

    The title is a Course's only human-readable handle, so a typo with no way to
    fix it would follow the project around.

    **Deliberately cannot touch `entries`.** The order moves through one route,
    so there is one place a reorder is validated, versioned and recorded. A
    second door onto the array would be a second place the version check has to
    be remembered, and the one that forgets is the one that loses an update.
    """
    ident, err = _course_writer(request)
    if err:
        return err
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        return refusals.refuse(400, "title is required")
    # Trimmed BEFORE the length check, for the reason `create_course` gives: the
    # check and the stored value have to agree on which string they mean, or a
    # title that fits after trimming is refused about a value nobody wrote.
    title = title.strip()
    if len(title) > store.MAX_TITLE:
        return refusals.refuse(
            400, f"title is longer than {store.MAX_TITLE} characters")
    try:
        status, body = await run_in_threadpool(
            _do_retitle_course, ident, course_id,
            store.clip(title, store.MAX_TITLE))
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    return JSONResponse(status_code=status, content=body)


def _do_retitle_course(ident, course_id: str, title: str):
    conn = store.connect_rw()
    try:
        # **THE WRITE LOCK BEFORE THE FROZEN CHECK** (reviewer, PR #14). Reading
        # the status and then writing on the strength of it is a check-then-act,
        # exactly like the slot check and the version check, and a SELECT takes
        # no lock. Today the window is unreachable because no route can move a
        # Course off `live` — the supersede route is the next one in this stack,
        # and the moment it lands a supersede racing a retitle between the read
        # and the write reopens the gap `_frozen_course` exists to close, in a
        # narrower and non-deterministic form: the read says live, the write
        # lands anyway. Closing it here rather than there, because a control that
        # arrives one PR after the thing that needs it is a control that was
        # missing for a PR.
        conn.execute("BEGIN IMMEDIATE")
        course = _load_course(conn, ident, course_id)
        if course is None:
            return _course_not_found()
        frozen = _frozen_course(course)
        if frozen:
            return frozen
        # **`version` IS NOT BUMPED, and that is a decision rather than an
        # omission.** It is the optimistic-concurrency token for the ORDER, and
        # nothing else reads it. Bumping it here would make a caller's held
        # version stale for a reason that has nothing to do with the array, so a
        # reorder it had already rebased would come back 409 against a rename it
        # never conflicted with. Orient does not bump it either.
        #
        # THE COST IS ON THE CLIENT SIDE AND IS WORTH STATING. A client caching
        # the whole Course keyed on `version`, treating it as a general-purpose
        # ETag, will not see a rename: `version` is exactly what such a client
        # polls, and a PATCH leaves it alone while `title` moves. `version` is
        # the order's own token and nothing else, and a client wanting to know
        # whether anything at all changed reads `updated_at`.
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        # CONDITIONAL ON THE STATUS IT READ, the same compare-and-swap the other
        # two write routes use. The lock above makes the sequence safe; this
        # makes it safe without depending on the lock.
        cur = conn.execute(
            "UPDATE courses SET title = ?, updated_by = ?, updated_at = ? "
            # `IS ?` RATHER THAN `= ?`, matching the supersede CAS. **There is
            # no equivalent live bug on this route and the swap is for shape
            # alone** (reviewer, PR #16). SQL equality against NULL is never true,
            # so `status = ?` bound to None would match no row — but a NULL status
            # never reaches this statement, because `_frozen_course` above refuses
            # every status that is not `live`. Spelled the same way as its sibling
            # so a reader comparing the two finds one idiom rather than a
            # difference to account for.
            "WHERE id = ? AND status IS ?",
            (title, ident.actor, now.isoformat(" "), course_id,
             course["status"]))
        if cur.rowcount != 1:
            conn.rollback()
            # Re-read through `_load_course`, so the scope check is not skipped
            # on the way out (reviewer, PR #15).
            fresh = _load_course(conn, ident, course_id)
            if fresh is None:
                return _course_not_found()
            return _frozen_course(fresh) or (409, {
                "error": "this Course changed since it was read",
                "hint": "re-read it and retry"})
        conn.execute(
            "INSERT INTO course_events (course_id, actor, action, entries, note, at) "
            "VALUES (?,?,?,?,?,?)",
            # `entries` stays NULL: this action did not move the order, and a
            # snapshot written here would make the history claim it did.
            # The new title goes in `note`, so the record says what it became
            # rather than only that a rename happened.
            (course_id, ident.actor, store.COURSE_ACTION_RETITLED, None, title,
             now.isoformat(" ")))
        conn.commit()
        fresh = _load_course(conn, ident, course_id)
        return 200, store.course_dict(fresh)
    finally:
        conn.close()


@app.post("/courses/{course_id}/supersede")
async def supersede_course(request: Request, course_id: str):
    """Retire a route, keeping it.

    A bead's destination is "temporarily final" (ADR-0024), so the route to a
    previous one is history worth having. This marks rather than deletes: a
    superseded Course still answers "what did we intend before", which a deleted
    row cannot.

    Two things follow from the mark and neither is incidental. The Course becomes
    frozen, so every write route refuses it. And it stops holding its product's
    live slot, so the next `POST /courses` succeeds.

    **An approved epic must not be stranded by the retirement** (the operator,
    2026-09-21): every epic on this Course whose `maturity` is `approved` must
    either land on the successor or already be resolved `wontfix` — an epic
    silently outliving the plan that approved it is the failure this closes.
    Pass `successor` — `{"title": ..., "entries": [...]}` — to hand the plan
    straight to its replacement; its `entries` takes the same membership rules
    `POST /courses` checks (only epics, only this product, no repeats). When any
    approved epic would otherwise be neither on `successor.entries` nor
    `wontfix`, the whole call is refused and nothing is written, `successor`
    included — a caller cannot half-retire a plan by naming a hand-off that
    does not actually cover what was approved on it. Creating the successor and
    retiring this Course commit together or not at all.

    **Superseding an already-superseded Course answers 200 rather than 409**, and
    says `idempotent` so a caller can tell the two apart. The request asked for a
    state the Course is already in, and refusing that would make a retried call
    after a dropped response look like a failure. `successor` is ignored here:
    the retirement already happened, so there is nothing left to hand off to.

    **A Course whose status is neither `live` nor `superseded` is retired and the
    anomaly is reported to the Archives** (reviewer, PR #16). That state should
    not exist — every write path here sets the column from a constant and only
    corruption elsewhere could produce it — so this route is the first place it
    gets a defined behaviour, and healing it into an ordinary-looking history
    entry would destroy the evidence that something upstream is broken. Retitle
    takes the opposite line on the same row and refuses it outright at
    `_frozen_course`. The asymmetry is deliberate: a rename of a corrupted plan is
    a change nobody needs, and retiring one is the act that stops it holding a
    product's live slot hostage. **`successor` is ignored on this path too**: the
    hand-off rule is for a deliberate retirement, and folding it into the
    recovery of a row this service did not corrupt would refuse the recovery
    over a rule that was never meant to gate it.
    """
    ident, err = _course_writer(request)
    if err:
        return err
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    note, err = _validate_course_note(payload)
    if err:
        return JSONResponse(status_code=err[0], content=err[1])
    successor, err = _validate_successor_field(payload)
    if err:
        return JSONResponse(status_code=err[0], content=err[1])
    try:
        status, body, anomaly = await run_in_threadpool(
            _do_supersede_course, ident, course_id, note, successor)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    if anomaly is not None:
        # AFTER THE WRITE COMMITTED, and best-effort: a failure recording the
        # anomaly must not undo the retirement that already happened.
        await _signal_course_status_anomaly(ident, anomaly)
    return JSONResponse(status_code=status, content=body)


async def _signal_course_status_anomaly(ident, detail: dict) -> None:
    """The Archives record for retiring a Course whose status was neither `live`
    nor `superseded`.

    Without it the `course_events` row reads exactly like an ordinary supersede,
    and an operator investigating a status anomaly later would be looking at a
    history that says nothing happened out of the ordinary. The signal is what
    makes the recovery visible.
    """
    row = {
        "id": f"sig_{store.new_ulid()}",
        "source": "library.courses",
        "kind": "course_status_anomaly_retired",
        "actor": ident.actor,
        "ingested_by": ident.actor,
        # NULL, AND EXPLICITLY SO. This row is the service reporting an anomaly
        # it noticed, not a capture made for a person, so there is no human to
        # attest. Set here rather than defaulted in `insert_signal`, so a future
        # caller that genuinely has a human cannot omit the field by accident.
        "acting_human": None,
        "product": detail["product"],
        "stream": "chart-house",
        "body": f"{detail['course_id']} was superseded by {ident.actor} from "
                f"status {detail['prior_status']!r}, which is neither "
                f"{store.COURSE_LIVE!r} nor {store.COURSE_SUPERSEDED!r}. Every "
                f"write path sets that column from a constant, so the row was "
                f"corrupted by something outside this service and the retirement "
                f"recovered it.",
        "refs": json.dumps({"course": detail["course_id"]}),
        "meta": json.dumps({"prior_status": detail["prior_status"]}),
        "privacy_class": "internal",
        "dedup_key": None,
        "produced_at": store.produced_now(),
    }
    try:
        await run_in_threadpool(store.insert_signal, row, lambda _ex: False)
    except Exception as e:                                   # noqa: BLE001
        print(f"library.courses: could not record the status anomaly: {e}",
              file=sys.stderr)


def _stranded_approved_epics(conn, course, covered: list) -> list[str]:
    """The approved epics on `course` the hand-off rule blocks the retirement
    on: still on this Course's own `entries`, still `maturity = approved`,
    named in neither `covered` nor already resolved `wontfix`.

    **The operator, 2026-09-21:** an epic that would not be on the superseding Course
    must either be added to it or closed won't-do; an approved epic is a
    commitment the ladder (ADR-0024) makes visible, and a plan quietly retiring
    out from under one erases that commitment with nothing to show it was ever
    made.

    Scoped to THIS Course's entries, not to every approved epic of the
    product — an epic approved onto some other, unrelated Course is that
    Course's commitment to keep, not this retirement's to check.
    """
    entries = store._json_or(course["entries"], [])
    if not entries:
        return []
    covered_ids = set(covered)
    placeholders = ",".join("?" * len(entries))
    rows = conn.execute(
        f"SELECT id, state, maturity FROM issue_items WHERE id IN "
        f"({placeholders})", entries)
    return sorted(r["id"] for r in rows
                 if r["maturity"] == store.ITEM_MATURITY_APPROVED
                 and r["id"] not in covered_ids
                 and r["state"] != "wontfix")


def _mark_predecessor_superseded(conn, course_id: str, prior_status: str,
                                 actor: str, now_iso: str) -> int:
    """The predecessor's compare-and-swap write, pulled out on its own so the
    hand-off's atomicity — this write and `_create_successor_course` commit
    together or not at all — can be tested by forcing one or the other to fail
    without reaching into SQL internals.

    **CONDITIONAL ON THE STATUS IT READ**, the same compare-and-swap shape the
    reorder route uses on `version`. The write lock `_do_supersede_course`
    takes makes the read-then-write safe; this makes it safe without depending
    on the lock, so a second supersede racing the first writes once rather
    than twice.
    """
    cur = conn.execute(
        "UPDATE courses SET status = ?, updated_by = ?, updated_at = ? "
        # **`IS ?` RATHER THAN `= ?`, so a NULL status compares rather than
        # vanishing** (reviewer, PR #16). SQL equality against NULL is never
        # true, so `status = ?` bound to None matches no row — including the
        # row whose status genuinely IS NULL. The CAS would then be a
        # permanent no-op that still answered 200, and this route would
        # report success on a row it can never write. `IS` is SQLite's
        # NULL-safe comparison and behaves identically for every other value.
        #
        # Only reachable through already-corrupted data: every write path
        # here hardcodes the status, and the column carries no NOT NULL. That
        # is the argument for fixing it rather than against — a response that
        # lies is worth more than the bug that produces it costs.
        "WHERE id = ? AND status IS ?",
        (store.COURSE_SUPERSEDED, actor, now_iso, course_id, prior_status))
    return cur.rowcount


def _create_successor_course(conn, ident, product: str, title: str,
                             entries: list, now_iso: str):
    """The hand-off's other half: a fresh live Course for `product`, inserted
    in the same transaction as the predecessor's retirement.

    **SAFE TO INSERT AS `live` WITHOUT RE-CHECKING THE SLOT.** By the time this
    runs, `_mark_predecessor_superseded` has already flipped the only live
    Course this product had to `superseded`, in the same held write
    transaction — so the partial unique index has nothing live to collide
    with. `_do_create_course`'s own pre-check exists for a caller with no
    Course to retire; this call always has one.
    """
    course_id = f"course_{store.new_ulid()}"
    conn.execute(
        "INSERT INTO courses (id, product, title, entries, status, "
        "version, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (course_id, product, store.clip(title, store.MAX_TITLE),
         json.dumps(entries), store.COURSE_LIVE, 1, ident.actor, now_iso))
    conn.execute(
        "INSERT INTO course_events (course_id, actor, action, entries, at) "
        "VALUES (?,?,?,?,?)",
        # Same NULL-for-empty rule `_do_create_course` writes: a brand-new
        # Course, even one born from a hand-off, never moved anywhere yet.
        (course_id, ident.actor, store.COURSE_ACTION_CREATED,
         json.dumps(entries) if entries else None, now_iso))
    return conn.execute("SELECT * FROM courses WHERE id = ?",
                        (course_id,)).fetchone()


def _do_supersede_course(ident, course_id: str, note, successor):
    conn = store.connect_rw()
    try:
        # THE WRITE LOCK BEFORE THE READ, as the other two write paths take it.
        # Retiring a Course frees its product's live slot, so this is the
        # check-then-act on the other side of `_do_create_course`'s: without the
        # lock a supersede and a create could interleave such that the create's
        # slot check reads a Course this call is about to retire, and refuses a
        # plan that would have been perfectly legal a moment later. It is also
        # what makes inserting the successor as `live` safe without a second
        # slot check — see `_create_successor_course`.
        conn.execute("BEGIN IMMEDIATE")
        course = _load_course(conn, ident, course_id)
        if course is None:
            return (*_course_not_found(), None)
        if course["status"] == store.COURSE_SUPERSEDED:
            # NO EVENT, NO TIMESTAMP MOVE. A second supersede recorded a second
            # time would put two retirements in the history for one act, and
            # `updated_at` would say the plan changed when nothing did.
            # `successor` is ignored: the retirement already happened, so
            # there is nothing left here to hand off.
            return 200, {**store.course_dict(course), "idempotent": True}, None
        # A THIRD STATE SHOULD NOT EXIST, so retiring a row in one is reported
        # rather than absorbed. The caller still gets an ordinary 200 — the plan
        # IS retired, which is what it asked for — and the Archives get the fact
        # that the row arrived here corrupted. Captured before the write, since
        # the write is what erases it.
        anomaly = None
        successor_entries = None
        if course["status"] != store.COURSE_LIVE:
            anomaly = {"course_id": course_id, "product": course["product"],
                       "prior_status": course["status"]}
            # `successor` IS NOT USED ON THIS PATH, deliberately. The hand-off
            # rule is for a deliberate retirement of a live plan; folding it
            # into the recovery of a row this service did not corrupt would
            # refuse the recovery over a rule that was never meant to gate it.
        else:
            if successor is not None:
                successor_entries, err = _validate_entries(
                    conn, ident, course["product"], successor["raw_entries"])
                if err:
                    return (*err, None)
            stranded = _stranded_approved_epics(conn, course,
                                                successor_entries or [])
            if stranded:
                return (409, {
                    "error": "an approved epic must be added to the "
                             "successor or resolved wontfix before this "
                             "Course is superseded",
                    "stranded": stranded,
                    "hint": "add these epics to successor.entries, or "
                            "resolve each wontfix first"}, None)
        now_iso = _dt.datetime.now(
            _dt.timezone.utc).replace(tzinfo=None).isoformat(" ")
        rowcount = _mark_predecessor_superseded(
            conn, course_id, course["status"], ident.actor, now_iso)
        if rowcount != 1:
            conn.rollback()
            # Re-read through `_load_course`, so the scope check is not skipped
            # on the way out — the same reason the reorder route's rollback path
            # takes it (reviewer, PR #15). The body this builds is a full Course.
            fresh = _load_course(conn, ident, course_id)
            if fresh is None:
                return (*_course_not_found(), None)
            # It is superseded now, by whoever won. The caller asked for this
            # state and the state is what it asked for, so this is the idempotent
            # answer rather than a conflict.
            #
            # **THE LOSER'S `note` IS DISCARDED, DELIBERATELY, and `idempotent`
            # is what tells it so** (reviewer, PR #16). Two callers retiring the
            # same Course for different stated reasons leave only the winner's
            # note in the history. Recording the loser's would put two
            # retirements in the log for one act, which is the same thing the
            # no-event rule above refuses; the flag is the caller's signal that
            # its own reason was not the one written down. The loser's
            # `successor`, if it named one, is discarded the same way — the
            # winner's retirement is the one that happened.
            return 200, {**store.course_dict(fresh), "idempotent": True}, None
        conn.execute(
            "INSERT INTO course_events (course_id, actor, action, entries, note, at) "
            "VALUES (?,?,?,?,?,?)",
            # `entries` stays NULL: retiring a route does not move its order, and
            # the order it retired with is still on the row itself.
            (course_id, ident.actor, store.COURSE_ACTION_SUPERSEDED, None, note,
             now_iso))

        new_course = None
        if successor_entries is not None:
            new_row = _create_successor_course(
                conn, ident, course["product"], successor["title"],
                successor_entries, now_iso)
            new_course = store.course_dict(new_row)

        conn.commit()
        fresh = _load_course(conn, ident, course_id)
        # `idempotent` IS PRESENT ON ALL THREE SHAPES rather than only on the two
        # where it is true (reviewer, PR #16). `body.get("idempotent")` reads the
        # same either way; a field that appears and disappears makes the three
        # responses harder to hold in one's head and harder to schema-validate.
        body = {**store.course_dict(fresh), "idempotent": False}
        if new_course is not None:
            body["successor"] = new_course
        return 200, body, anomaly
    finally:
        conn.close()


def _validate_waypoint_members(conn, ident, product: str, raw):
    """A synthetic waypoint's `members` against the membership rules, or a
    `(status, body)` refusal — `_validate_entries`'s own rules, carried over
    where they still apply.

    **A MEMBER MAY BE ANY ITEM, EPIC OR WORK ITEM**, unlike a Course entry.
    The work item that specifies this is explicit: "a member of a synthetic
    waypoint is an item id (epic or work item) in the Course's product." So
    there is no `not_epics` check here — `_validate_entries`'s one rule that
    does not carry over.

    **DUPLICATES ARE REFUSED, for a different reason than on `entries`.**
    There, a repeated epic has no defined position in an ordered list. A
    member set has no position to lose — the reason here is that
    `store.waypoint_progress` counts every member once toward `total`, so a
    repeated id would be counted twice toward both `total` and, once
    terminal, `terminal`, skewing `percent_terminal` for an id that names
    one thing.

    Every refusal names ALL the offending ids rather than the first, matching
    `_validate_entries`.
    """
    if not isinstance(raw, list):
        return None, (400, {"error": "members must be an array of item ids"})
    if not raw:
        return None, (400, {
            "error": "a synthetic waypoint must name at least one member"})
    if len(raw) > store.MAX_WAYPOINT_MEMBERS:
        return None, (400, {
            "error": f"members may hold at most "
                     f"{store.MAX_WAYPOINT_MEMBERS} items"})
    members = []
    for m in raw:
        if not isinstance(m, str) or not m.strip():
            return None, (400, {
                "error": "every member must be a non-empty item id"})
        members.append(m.strip())
    dupes = sorted({m for m in members if members.count(m) > 1})
    if dupes:
        return None, (400, {
            "error": "an item may appear in a synthetic waypoint only once",
            "duplicates": dupes})
    scope, scope_params = _scope_clause(ident)
    rows = {r["id"]: r for r in conn.execute(
        f"SELECT id, product FROM issue_items "
        f"WHERE id IN ({','.join('?' * len(members))}){scope}",
        [*members, *scope_params])}
    unknown = sorted(m for m in members if m not in rows)
    if unknown:
        return None, (400, {
            "error": "no such work item, or none this identity may see",
            "unknown": unknown})
    foreign = sorted(m for m in members if rows[m]["product"] != product)
    if foreign:
        return None, (400, {
            "error": f"every member must belong to product {product!r}",
            "wrong_product": foreign})
    return members, None


@app.post("/courses/{course_id}/waypoints")
async def add_course_waypoint(request: Request, course_id: str):
    """Name a synthetic waypoint: a set of items, epics or work items, that
    must all complete before this point on the Course is reached.

    **ADDITIVE, AND DELIBERATELY NOT PART OF `entries`.** `entries` is
    protocol-pinned to epic id strings (§6.1); this is the new, separate
    place a synthetic waypoint lives, so an old client reading `entries`
    sees exactly what it always has. `GET /courses/{id}` reports each one's
    members and their states under `waypoint_progress`, and places it in
    `position["synthetic_waypoints"]` — "epic" and "waypoint" are defined
    once, in `lease.py`'s module docstring.

    **`version` IS NOT TOUCHED, for the reason `retitle_course` gives for the
    same choice.** It is the optimistic-concurrency token for `entries`
    alone; this writes a different column, so bumping it would 409 a
    reorder that never conflicted with this write.
    """
    ident, err = _course_writer(request)
    if err:
        return err
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    try:
        status, body = await run_in_threadpool(
            _do_add_course_waypoint, ident, course_id, payload.get("members"))
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    return JSONResponse(status_code=status, content=body)


def _do_add_course_waypoint(ident, course_id: str, raw_members):
    conn = store.connect_rw()
    try:
        # THE WRITE LOCK BEFORE THE READ, the same reason every other Course
        # write takes it: reading the current `waypoints` array and appending
        # to it is a check-then-act, and a SELECT takes no lock.
        conn.execute("BEGIN IMMEDIATE")
        course = _load_course(conn, ident, course_id)
        if course is None:
            return _course_not_found()
        frozen = _frozen_course(course)
        if frozen:
            return frozen
        members, err = _validate_waypoint_members(
            conn, ident, course["product"], raw_members)
        if err:
            return err
        existing = store.course_dict(course)["waypoints"]
        if len(existing) >= store.MAX_COURSE_WAYPOINTS:
            return 400, {
                "error": f"a Course may hold at most "
                         f"{store.MAX_COURSE_WAYPOINTS} synthetic waypoints"}
        waypoint_id = f"waypoint_{store.new_ulid()}"
        updated = existing + [{"id": waypoint_id, "members": members}]
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        # CONDITIONAL ON THE STATUS IT READ, matching `_do_retitle_course`'s
        # CAS — the lock above makes the sequence safe, this makes it safe
        # without depending on the lock.
        cur = conn.execute(
            "UPDATE courses SET waypoints = ?, updated_by = ?, updated_at = ? "
            "WHERE id = ? AND status IS ?",
            (json.dumps(updated), ident.actor, now.isoformat(" "), course_id,
             course["status"]))
        if cur.rowcount != 1:
            conn.rollback()
            # Re-read through `_load_course`, so the scope check is not
            # skipped on the way out, matching every other write here.
            fresh = _load_course(conn, ident, course_id)
            if fresh is None:
                return _course_not_found()
            return _frozen_course(fresh) or (409, {
                "error": "this Course changed since it was read",
                "hint": "re-read it and retry"})
        conn.execute(
            "INSERT INTO course_events (course_id, actor, action, waypoints, "
            "at) VALUES (?,?,?,?,?)",
            (course_id, ident.actor, store.COURSE_ACTION_WAYPOINT_ADDED,
             json.dumps(updated), now.isoformat(" ")))
        conn.commit()
        fresh = _load_course(conn, ident, course_id)
        return 201, store.course_dict(fresh)
    finally:
        conn.close()


def _claim_owner(payload: dict, ident) -> tuple[str | None, JSONResponse | None]:
    """The claim's owner — WHO holds this item — or a refusal.

    **Deliberately not the authenticated principal.** Every co session's tools
    present the same bearer token, so the token identity is one string for a
    daytime co and for the overnight drain. Recording that as the owner made a
    claim unattributable: the queue could not show who held an item, a session
    could not release only its own claims, and the overnight could not tell a live
    daytime claim from its own stale one from last night. So the caller names
    itself, and an absent name falls back to the token identity, which keeps
    existing callers working at the old, coarser granularity.

    **The owner is a COORDINATION label, not an authorization principal.** It says
    who to attribute the claim to and nothing about who may act: authorization
    stays entirely with the token, its capability and its product scope, all
    checked separately. A caller naming itself something else gains no access it
    did not have; the worst it can do is misattribute or release a claim inside a
    product it can already write to, and the event log records the real actor
    either way.
    """
    raw = payload.get("owner")
    if raw is None:
        return ident.actor, None
    if not isinstance(raw, str) or not raw.strip():
        return None, refusals.refuse(400, "owner must be a non-empty string")
    return store.clip(raw.strip(), store.MAX_ACTOR), None


def _row_crew(row, column: str) -> str | None:
    """`row[column]`, or `None` when `row`'s own schema carries no such
    column.

    A row from a schema predating this column means exactly what an explicit
    `NULL` already means to `_crew_mismatch` below: no crew was recorded.
    That covers every row written before the migration that added the
    column, and it is also what keeps a test fixture that mirrors the
    production schema by hand, but was written before this change, resolving
    rather than raising `IndexError` on `row[column]`.
    """
    try:
        return row[column]
    except IndexError:
        return None


def _crew_mismatch(ident, recorded_crew: str | None) -> bool:
    """Whether `ident` is refused from acting on a hold recorded as taken by
    `recorded_crew` (CQ·10, the operator's ruling of 2026-10-15, comment 5378 on
    issue_01KZMQCBXQ5KF2DTJCV3A9GMF7).

    **False whenever there is nothing to compare.** No crew was recorded —
    every hold taken before any credential names a crew, and any taken since
    by a credential with no crew of its own — keeps today's behaviour
    exactly: the `owner`-string check downstream of this call still runs.
    `ident.kind == "approved_client"` also answers False unconditionally: that
    is the human-attested credential ADR-0026 reserves the live-lease-breaking
    force path to, and this ruling narrows releases and resolves between
    AGENT crews, not that reservation.

    **True for a non-human credential whose own crew differs from the
    recorded one — INCLUDING a credential with no crew at all.** That is the
    ruling's own wording, and it is what closes the door the `owner` string
    alone cannot: an attacker who reads the recorded holder's name off the
    item, a lease listing or a 409 body and asserts it as `owner` matches
    that string exactly as the real holder would, and this check never reads
    `owner` at all.
    """
    return (recorded_crew is not None
            and ident.kind != "approved_client"
            and ident.crew != recorded_crew)


def _crew_mismatch_refusal() -> dict:
    """The 409 body for `_crew_mismatch`. Names neither crew — the ruling's
    own requirement — so a caller outside the holding crew learns nothing
    about which crew holds this beyond the bare fact that it is not theirs.
    """
    return {"error": "this hold was taken by a credential of a different crew",
            "reason": "crew_mismatch"}


# `CONTROLLED_REFS`/`_strip_controlled_refs` are GONE
# (issue_01M0ASMCB4F68PF3NJG7BV7K6C). `decision_id` was the one controlled
# key, stripped from caller-supplied `refs` because `escalate` was its only
# legitimate writer and `_return_escalated_issues` depended on that being
# true. Both are gone with the decision plane — nothing left reads
# `refs.decision_id` for anything, so there is nothing left to protect by
# stripping it.


# `_decision_is_about` and `_decision_is_about_sql` are GONE
# (issue_01M0EEPS591VQHYEBJY7NGF4C3). Both existed to close a self-approval
# hole in the ARCH-403 feature gate: a decision's OWN record had to name the
# item back, so an agent could not harvest an unrelated approved decision id
# and use it to check out its own feature. `feature` is no longer a work-item
# type, so there is no gate left for either function to protect.


def _lease_refusal(conflict, *, visible: bool = True) -> dict:
    """The refusal body for a checkout a live lease turned away.

    **IT NAMES THE HOLDER AND THE EXPIRY, and that is the requirement rather than
    a nicety.** A bare "no" leaves an orchestrator two options, block or retry,
    and both waste the run. "Epic E is held by co-vivy-0912 until 05:30, covering
    these four items" lets it route elsewhere and come back.

    `blocks_by` separates the two directions a fence arrives from, because they
    need different responses. An `ancestor` conflict means someone above already
    covers this item, so there is nothing smaller to take. A `descendant` conflict
    means the caller reached for an epic while someone works inside it, and a
    different branch may well succeed.
    """
    held = conflict.lease
    where = ("an epic above this item is held"
             if conflict.blocks_by == "ancestor"
             else "something beneath this epic is already held")
    if not visible:
        # **THE BLOCKING LEASE'S OWN PRODUCT IS CHECKED, NOT ONLY THE REQUESTED
        # ITEM'S** (security-reviewer, PR #7, raised twice). The caller is scoped
        # to the item it named; the lease blocking it sits on a different row,
        # and nothing had checked that row's product. A cross-product parent
        # would hand a caller in product A a real item id and an actor name in
        # product B, across the boundary `in_scope` exists to enforce.
        #
        # Unreachable today, since no route can create a cross-product parent —
        # which is exactly the argument that ages badly. The re-parent surface is
        # already reserved in the contract, and this repo has produced this
        # corruption class once before, in Course entries, closed the same way:
        # report the obstruction, disclose nothing about it.
        return {
            "error": f"{where}, and it is outside this identity's scope",
            "reason": "held",
            "hint": "the fence is real but its holder is not yours to see; route "
                    "to work outside this subtree",
        }
    return {
        "error": f"{where}: {held['root_id']} is leased by {held['holder']} "
                 f"until {store._iso(lease.expires_at(held['taken_at'], held['heartbeat_at']))}",
        "reason": "held",
        "lease": lease.lease_dict(held),
        "hint": "route to work outside this subtree — an agent may reclaim a "
                "lease whose holder is gone, but only a human may break a "
                "live one, and no route here does that",
    }


@app.post("/items/{item_id}/resolve")
async def resolve_item(request: Request, item_id: str):
    """End a work item, and end the hold on its subtree with it.

    **The item must be the caller's to change** (`_hold_for_write`, the operator
    2026-09-11): held under `owner`, or held by nobody, in which case this
    resolve holds it for its own write. Another holder's live lease refuses
    with `reason: "held"`.

    **Resolving releases the lease, and that is not tidiness.** An epic otherwise
    stays fenced for the whole grace period after the work is done — the same
    stranding `/release` exists to prevent, arriving by a different door, and the
    likelier one: an agent that fixes an item resolves it and moves on, and few
    release first.

    **It releases the hold it has, and refuses the one it does not — against
    error, not against an adversary.** A live lease on this item held by another
    owner refuses the resolve; a dead one releases, because reclaiming a dead
    lease needs no human and that is the distinction ADR-0026 draws.

    **THE GATE COMPARES TWO SELF-DECLARED STRINGS AND AN ADVERSARY WALKS PAST IT**
    (security-reviewer, PR #9; `issue_01KZMQCBXQ5KF2DTJCV3A9GMF7`). `owner` is
    caller-supplied with no binding to the token, and `holder` is the same kind of
    string. Reading `claimed_by` off the item and asserting it costs an attacker
    one request. So this stops a caller that forgets to name itself and nothing
    more, and saying so plainly is the point — a partial mitigation described as
    a fix is how the next reader stops looking.

    Worse here than at `/release`, which only drops a fence early and leaves the
    item recoverable: this drives the item into a terminal state no later call can
    undo, attributed to someone who never held the work. It must be resolved
    before `LIBRARY_WRITES_ENABLED=1`; Phase 5's per-session tokens are what make
    the holder verifiable.

    An earlier version forced unconditionally, on the reasoning that a terminal
    state ends the hold regardless of who asks. That is circular — the work is
    live until this call makes it terminal — and it made this the route through
    which any in-scope caller could end another session's work and unfence its
    epic, which the ADR reserves to a human wherever it can happen.

    The release is recorded as `resolved` rather than `released`, so "finished
    without releasing" stays distinguishable from a clean hand-back.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "item.resolve"):
        return refusals.forbidden("item.resolve")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")

    resolution = payload.get("resolution")
    if resolution not in store.ITEM_RESOLUTIONS:
        return refusals.refuse(
            400, "resolution is not one of the resolutions this vault defines",
            supported=sorted(store.ITEM_RESOLUTIONS))
    note = payload.get("outcome_note")
    if note is not None and not isinstance(note, str):
        return refusals.refuse(400, "outcome_note must be a string when present")
    if resolution == "redirected" and not (note or "").strip():
        # A redirect that does not say where is a dead end wearing the shape of a
        # handoff.
        return refusals.refuse(
            400, "outcome_note is required when resolution is redirected")
    refs = payload.get("refs")
    if refs is not None and not isinstance(refs, dict):
        return refusals.refuse(400, "refs must be an object")
    # The same self-declared label `/release` uses, and for the same reason: the
    # server cannot tell one session from another, so a caller names itself. It
    # is attribution, not authorization — every capability and scope check is
    # made against the token independently.
    owner, err = _claim_owner(payload, ident)
    if err:
        return err

    try:
        result = await run_in_threadpool(_do_resolve, ident, item_id, resolution,
                                         note, refs, owner)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    status, body = result
    return JSONResponse(status_code=status, content=body)


def _do_resolve(ident, item_id: str, resolution: str, note, refs, owner):
    conn = store.connect_rw()
    try:
        item = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                            (item_id,)).fetchone()
        if item is None or not identity.in_scope(ident, item["product"]):
            return 404, {"error": "no such item"}
        if item["state"] in store.ITEM_TERMINAL:
            return 409, {"error": f"already {item['state']}"}
        refusal = _hold_for_write(conn, ident, item, owner, act="a resolve")
        if refusal:
            return refusal

        # **A LIVE LEASE HELD BY SOMEONE ELSE REFUSES THE RESOLVE**
        # (security-reviewer, PR #9). This route forced the release
        # unconditionally, which made it the one door ADR-0026 names: any token
        # with the stock `item.resolve` grant could mark another session's
        # in-flight work terminal and unfence its whole epic, with no human in
        # the loop and no `force` flag to refuse — the reserved act, reached
        # through the route that was not checking.
        #
        # My justification for forcing was circular. The work is LIVE until this
        # very call makes it terminal, so "there is no live work left to protect"
        # assumes the conclusion. `/release` and `/checkout` both refuse `force`
        # for a non-human; this performed the same break with no flag at all.
        #
        # A DEAD lease still releases, because reclaiming one needs no human —
        # that is the distinction ADR-0026 draws, and it is the whole reason a
        # lease has an expiry.
        #
        # DIVERGES FROM ORIENT, which forces here unconditionally. ADR-0026 is a
        # recorded decision and parity does not outrank it; the Orient gap is
        # filed rather than mirrored.
        # **THE SAME BIDIRECTIONAL WALK CHECKOUT USES, because reimplementing it
        # narrower is how this route acquired three holes in a row**
        # (security-reviewer, PR #9). `find_conflict` looks up the ancestor chain
        # AND down the subtree; my hand-rolled versions looked at one row.
        #
        # First they were scoped to `taken_for`, so resolving an EPIC skipped the
        # check while a child was held. Then they were one `live_lease_on` on the
        # item's own root, which still missed anything more than a hop away: a
        # holder of the grandparent did not block resolving a leaf, and a holder
        # of the leaf's parent did not block resolving the grandparent. Both let
        # an uninvolved caller end another session's work and leave the fence
        # dangling.
        #
        # ADR-0026 already settled what "overlapping" means and `find_conflict`
        # already implements it. Resolve now sees exactly what checkout refuses
        # to overlap, which is the property that was wanted all along.
        #
        # What may be RELEASED still takes `taken_for`, and that asymmetry is
        # deliberate: a session holding an epic across several children must not
        # lose its fence to any one of them finishing.
        try:
            # **EVERY conflict, not the first.** `find_conflict` returns one, and
            # `take` gets away with that because it loops, breaking one per pass.
            # A check has no loop, and reading the first hit as the whole answer
            # let a caller name whichever holder it had been told about and sail
            # past a second live one on a sibling branch — and the refusal below
            # discloses a holder's name, so one refused attempt was enough to
            # learn it (reviewer, PR #9).
            conflicts = lease.live_conflicts(conn, lease.root_id_for(conn, item))
        except lease.SubtreeTooLarge as too_big:
            # The same refusal checkout gives: a fence whose reach cannot be
            # measured must not be reasoned about.
            return 409, {"error": f"cannot enumerate this subtree: {too_big}",
                         "hint": "the tree beneath this item is malformed or far "
                                 "larger than any real epic; investigate before "
                                 "ending anything beneath it"}
        foreign = [c for c in conflicts if c.lease["holder"] != owner]
        if foreign:
            held = foreign[0].lease
            return 409, {
                "error": f"a live lease on {held['root_id']} is held by "
                         f"{held['holder']}",
                "reason": "held",
                "hint": "resolving would end another session's hold on this "
                        "subtree, which only a human may do; release it first if "
                        "it is yours",
            }
        # **A CONFLICT WHOSE `holder` STRING MATCHES `owner` IS STILL FOREIGN
        # WHEN THE CREW DOES NOT** (CQ·10). `foreign` above already refuses
        # every conflict the owner string alone catches; this is the one it
        # cannot — an attacker who read a sibling or ancestor lease's real
        # holder name and typed it back as `owner` passes that filter outright.
        # Checked after, not folded into the filter above, so the ordinary
        # "held by" refusal still wins when both are true at once, and this
        # narrower refusal fires only for the case nothing else catches.
        for c in conflicts:
            if (c.lease["holder"] == owner
                    and _crew_mismatch(ident, _row_crew(c.lease, "holder_crew"))):
                return 409, _crew_mismatch_refusal()

        merged = store._json_or(item["refs"], {})
        if refs:
            incoming = dict(refs)
            dupe = incoming.get("duplicate_of")
            if dupe is not None:
                dupe = dupe.strip() if isinstance(dupe, str) else dupe
                incoming["duplicate_of"] = dupe
                if not isinstance(dupe, str) or len(dupe) > 40:
                    return 400, {"error": "duplicate_of must be an item id"}
                if dupe == item_id:
                    return 400, {"error": "an item cannot duplicate itself"}
                # **VALIDATED, AND VALIDATED IN THE CALLER'S SCOPE.** A duplicate
                # pointing at an id that does not exist is worse than no link: it
                # reads as a resolved relationship and sends the next reader
                # after an item that was never there. Checking in scope is what
                # stops this becoming an existence probe for other products —
                # the same rule the 404 above follows.
                target = conn.execute(
                    "SELECT product FROM issue_items WHERE id = ?",
                    (dupe,)).fetchone()
                if target is None or not identity.in_scope(ident,
                                                           target["product"]):
                    return 400, {"error": f"no such item: {dupe}"}
            merged = {**merged, **incoming}

        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        prev = item["state"]
        outcome = (store.clip(note, store.MAX_ITEM_TEXT)
                   if isinstance(note, str) and note.strip() else None)
        # **THE UPDATE IS CONDITIONAL ON THE STATE THIS REQUEST READ, and the
        # rowcount decides the winner** — the compare-and-swap decide/consume's
        # round-2 review (PR #26) applied, reached here through the shared
        # writer that records the transition. The SELECT above takes no lock and
        # the terminal check is Python-only, so two concurrent resolutions of
        # one `open` item can both pass them; without a predicate the second
        # UPDATE would overwrite the first and `issue_events` would end up with
        # two rows each claiming a transition out of `open`, the row landed on
        # whichever committed last. The loser rolls back — no event, no release
        # forced — and is told the state it collided with.
        cur = conn.execute(
            "UPDATE issue_items SET state = ?, resolution = ?, resolved_by = ?, "
            "resolved_at = ?, outcome_note = ?, refs = ? "
            "WHERE id = ? AND state = ?",
            (resolution, resolution, ident.actor, now.isoformat(" "), outcome,
             json.dumps(merged), item_id, prev))
        if cur.rowcount != 1:
            conn.rollback()
            fresh = conn.execute("SELECT state FROM issue_items WHERE id = ?",
                                 (item_id,)).fetchone()
            if fresh is None:
                return 404, {"error": "no such item"}
            # **NO `item.read` GATE BELONGS ON THIS STATE NAMING: THE ROUTE IS
            # THE READER.** The `SELECT *` above handed the full row to any
            # in-scope `item.resolve` holder (`item.read` is never consulted),
            # the 200 path returns it, and the terminal `already {state}`
            # refusal above says the same with no race at all. This tells a CAS
            # loser which concurrent write won — state it had read to attempt
            # the transition. A role holding a write capability without
            # `item.read` is gated at the route, not here.
            return 409, {
                "error": f"the item is no longer {prev}; it is now "
                         f"{fresh['state']}",
                "hint": "re-read the item and retry against its current state"}
        conn.execute(
            "INSERT INTO issue_events (item_id, actor, from_state, to_state, at, "
            "note) VALUES (?,?,?,?,?,?)",
            (item_id, ident.actor, prev, resolution, now.isoformat(" "), outcome))
        # **RELEASED ONLY IF IT IS OURS OR IT IS DEAD** (security-reviewer, PR #9).
        # The refusal above has already established that; forcing here is safe
        # because the alternative has been excluded rather than assumed.
        #
        # The previous version passed `holder=item["claimed_by"] or ident.actor`
        # with `force=True`, which read as if the claimant gated the release and
        # did nothing at all — `force` short-circuits the only branch that reads
        # `holder`. Dead code that describes a control it does not perform is
        # worse than no comment.
        released = lease.release_for(
            conn, item, holder=owner, actor=ident.actor,
            reason=lease.LEASE_RESOLVED, force=True)
        conn.commit()
        fresh = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                             (item_id,)).fetchone()
        out = store.item_dict(
            fresh, claim=lease.claim_status(conn, fresh),
            prerequisites=store.prerequisite_status(
                conn, fresh, in_scope=_scope_predicate(ident)))
        out["lease_released"] = lease.lease_dict(released) if released else None
        return 200, out
    finally:
        conn.close()


def _refuse_unless_active(item, *, terminal: str | None = None):
    """A work route moves only an item in an `ITEM_ACTIVE` state.

    Every stored state is active or terminal since `parked` was retired, so a
    terminal item is the refusal this gives in practice. The last branch names
    any other state rather than letting it through, because a row carrying a
    retired state would otherwise be moved as if it were live.

    Returns a `(status, body)` refusal, or `None` when the item may move.
    """
    state = item["state"]
    if state in store.ITEM_ACTIVE:
        return None
    if state in store.ITEM_TERMINAL:
        return 409, {"error": terminal or f"already {state}"}
    return 409, {"error": f"state {state} is not one work can move"}


def _transition(conn, item_id: str, actor: str, prev: str, to: str,
                note: str | None, add_refs: dict | None = None,
                **columns) -> tuple[int, dict] | None:
    """Move an item's state, merge refs, and append the event, in one place.

    `link-pr` and `reconcile-pr`'s `merged`/`closed` outcomes are its callers.
    Keeping the write shared means the next route to move a state gets the event
    and the compare-and-swap without re-deriving them, and a state change with no
    event is a row the audit trail cannot explain.

    **Returns `None` on success, or the `(status, body)` refusal when another
    writer moved the row out of `prev` between the caller's read and this
    write** — the caller relays it. The UPDATE is conditional on `prev`, the
    state the caller's guards judged, so a losing concurrent writer rolls back
    and is told what it collided with rather than silently overwriting the
    winner (the compare-and-swap decide/consume's round-2 review applied to the
    shared writer, so no caller can forget the predicate and one fix covers the
    three).
    """
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    sets, params = ["state = ?"], [to]
    for col, value in columns.items():
        sets.append(f"{col} = ?")
        params.append(value)
    if add_refs:
        existing = store._json_or(
            conn.execute("SELECT refs FROM issue_items WHERE id = ?",
                         (item_id,)).fetchone()["refs"], {})
        sets.append("refs = ?")
        params.append(json.dumps({**existing, **add_refs}))
    cur = conn.execute(
        f"UPDATE issue_items SET {','.join(sets)} WHERE id = ? AND state = ?",
        [*params, item_id, prev])
    if cur.rowcount != 1:
        conn.rollback()
        fresh = conn.execute("SELECT state FROM issue_items WHERE id = ?",
                             (item_id,)).fetchone()
        if fresh is None:
            return 404, {"error": "no such item"}
        # **NO `item.read` GATE BELONGS ON THIS STATE NAMING: THE CALLING ROUTE
        # IS THE READER.** Each caller (link-pr, reconcile-pr) fetched the full
        # row before invoking this writer and returns it on success; `item.read`
        # is never consulted on those routes. This tells a CAS loser which
        # concurrent write won — state the caller had read to judge `prev`. A
        # role holding a write capability without `item.read` is gated at the
        # calling route, not here.
        return 409, {
            "error": f"the item is no longer {prev}; it is now {fresh['state']}",
            "hint": "re-read the item and retry against its current state"}
    conn.execute(
        "INSERT INTO issue_events (item_id, actor, from_state, to_state, at, "
        "note) VALUES (?,?,?,?,?,?)",
        (item_id, actor, prev, to, now.isoformat(" "), note))
    return None


def _refuse_unless_terminal(item):
    """`/reopen` moves only an item a resolve, wontfix or duplicate already
    closed.

    The mirror of `_refuse_unless_active`: an item still active has nothing to
    undo, and the refusal names its own state rather than assuming `open`.
    """
    state = item["state"]
    if state in store.ITEM_TERMINAL:
        return None
    return 409, {"error": f"item is {state}, not terminal; only a terminal "
                          f"item can be reopened"}


@app.post("/items/{item_id}/reopen")
async def reopen_item(request: Request, item_id: str):
    """Undo a wrong resolve: move a terminal item back to `open`.

    **The one door back.** `/correct` refuses `state` by name and `/revision`
    edits content only, so before this route a resolve that turned out wrong —
    a bad PR reconcile, a wrong multi-repo close — had no way back but filing a
    duplicate (`issue_01M3P06BYH15H2QVYFTHAMXHF4`).

    **Gated on `item.resolve`, the same capability `/resolve` and `/correct`
    use.** Reopening is the state write those routes already gate, run in
    reverse, not a new kind of change needing its own grant.

    **Held under the caller's checkout exactly like `/resolve`, `/correct` and
    `/link-pr`** (`_hold_for_write`, the operator 2026-09-11): the caller's own live
    lease, no live lease at all (held for this one write), or refused 409
    `reason: "held"` against another holder's.

    **A compare-and-set on the state this request read, through the same
    `_transition` `/link-pr` and `/reconcile-pr` share.** A concurrent write
    landing between the read and this UPDATE loses cleanly with 409, told the
    state it collided with, rather than overwriting the winner's row.

    **`reason` is required and non-empty, and it is the event's note.** A
    reopen with no reason is the unexplained state flip the audit trail exists
    to prevent.

    **Refused on anything but a terminal item, naming its current state** — an
    active item has nothing to undo.

    **`pr` and `pr_url` are left alone**, so the history still shows which PR
    the wrong close came from. `resolution`, `resolved_by`, `resolved_at` and
    `outcome_note` are cleared, since they describe a resolve this item no
    longer carries. `claimed_by` and `claimed_at` are cleared too, the same
    shape `/reconcile-pr`'s `merged`/`closed` outcomes already give a
    `pr-open` item returned to `open` — a row back in the pool should not read
    as still claimed by whoever last held it.

    **Additive within protocol version 1** (ADR-0021): a client built before
    this route simply never calls it; nothing about `/resolve`, `/correct` or
    any existing response shape changes underneath it.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "item.resolve"):
        return refusals.forbidden("item.resolve")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return refusals.refuse(400,
                               "reason is required and must be a non-empty "
                               "string")
    owner, err = _claim_owner(payload, ident)
    if err:
        return err
    try:
        result = await run_in_threadpool(_do_reopen, ident, item_id,
                                         reason.strip(), owner)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    status, body = result
    return JSONResponse(status_code=status, content=body)


def _do_reopen(ident, item_id: str, reason: str, owner: str):
    conn = store.connect_rw()
    try:
        item = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                            (item_id,)).fetchone()
        if item is None or not identity.in_scope(ident, item["product"]):
            return 404, {"error": "no such item"}
        refusal = _refuse_unless_terminal(item)
        if refusal:
            return refusal
        # **THE CREW CHECK RUNS AGAINST THE ITEM'S OWN `claimed_by_crew`,
        # NOT ONLY `_hold_for_write`'S LEASE** (CQ·10). `/resolve` already
        # released the lease that would otherwise gate this through
        # `_hold_for_write`, so by the time an item is terminal there is
        # usually no live lease left for that check to see.
        # `claimed_by_crew` survives a resolve — only reopen, release and
        # `/reconcile-pr` clear it — so it is what an attacker who read
        # `claimed_by` off this terminal item and typed it back as `owner`
        # would otherwise walk straight past.
        if _crew_mismatch(ident, _row_crew(item, "claimed_by_crew")):
            return 409, _crew_mismatch_refusal()
        refusal = _hold_for_write(conn, ident, item, owner, act="a reopen")
        if refusal:
            return refusal
        note = store.clip(reason, store.MAX_ITEM_TEXT)
        # `claimed_by`/`claimed_at` are cleared the same way `/reconcile-pr`'s
        # `merged`/`closed` outcomes clear them on a `pr-open` item returned to
        # `open` (app.py, `_do_reconcile_pr`): the item is handed back to the
        # pool, and a reader should not see a stale claimant's name on a row
        # that reads as open.
        refusal = _transition(
            conn, item_id, ident.actor, item["state"], store.ITEM_STATE_OPEN,
            note, resolution=None, resolved_by=None, resolved_at=None,
            outcome_note=None, claimed_by=None, claimed_at=None,
            claimed_by_crew=None)
        if refusal:
            return refusal
        conn.commit()
        fresh = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                             (item_id,)).fetchone()
        return 200, store.item_dict(
            fresh, claim=lease.claim_status(conn, fresh),
            prerequisites=store.prerequisite_status(
                conn, fresh, in_scope=_scope_predicate(ident)))
    finally:
        conn.close()


# `/items/{id}/escalate` is GONE (issue_01M0ASMCB4F68PF3NJG7BV7K6C) — it
# existed to hand an item to the decision plane, which no route can create a
# fresh entry in anymore. An item that needs a human sets `needs="operator"`
# instead, the pattern already used everywhere else.


# `/items/{id}/park` and `/unpark` are GONE, and `parked` with them (ADR
# platform/decisions/0074). A parked item hid its blocker inside a state and a
# prose condition nothing could check; a blocker is now a `prerequisite`, and
# `blocked` is derived from it at read time. The 33 items with park history
# keep `park_reason`, `unpark_condition` and `parked_at` as history, and their
# park events stay in `issue_events`.


@app.post("/items/{item_id}/link-pr")
async def link_pr_to_item(request: Request, item_id: str):
    """Record that a fix PR is open for this item. A terminal item is refused."""
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "item.claim"):
        return refusals.forbidden("item.claim")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    pr = payload.get("pr")
    if isinstance(pr, bool) or pr is None:
        return refusals.refuse(400, "pr is required")
    if not isinstance(pr, (int, str)):
        return refusals.refuse(400, "pr must be a number or string")
    if isinstance(pr, str) and not pr.strip():
        return refusals.refuse(400, "pr is required")
    add_refs = {"pr": store.clip(pr.strip(), store.MAX_REF)
                if isinstance(pr, str) else pr}
    url = payload.get("url")
    if url is not None:
        if not isinstance(url, str):
            return refusals.refuse(400, "url must be a string when present")
        if url.strip():
            add_refs["url"] = store.clip(url.strip(), store.MAX_REF)
    owner, err = _claim_owner(payload, ident)
    if err:
        return err
    try:
        result = await run_in_threadpool(_do_link_pr, ident, item_id, add_refs,
                                         owner)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    status, body = result
    return JSONResponse(status_code=status, content=body)


def _do_link_pr(ident, item_id: str, add_refs: dict, owner: str):
    conn = store.connect_rw()
    try:
        item = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                            (item_id,)).fetchone()
        if item is None or not identity.in_scope(ident, item["product"]):
            return 404, {"error": "no such item"}
        refusal = (_refuse_unless_active(item)
                   or _hold_for_write(conn, ident, item, owner,
                                      act="linking a PR"))
        if refusal:
            return refusal
        refusal = _transition(conn, item_id, ident.actor, item["state"],
                              store.ITEM_STATE_PR_OPEN, None, add_refs)
        if refusal:
            return refusal
        conn.commit()
        fresh = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                             (item_id,)).fetchone()
        return 200, store.item_dict(
            fresh, claim=lease.claim_status(conn, fresh),
            prerequisites=store.prerequisite_status(
                conn, fresh, in_scope=_scope_predicate(ident)))
    finally:
        conn.close()


# The three endings `/reconcile-pr` accepts, per `issue_01M2HRVPQN9X8WN3KA0W9S9P30`
# (the operator, Deck session, 2026-09-14). The caller — agent-platform's
# `bin/issue-pr-reconcile` — is the one that polls the linked PR and knows which
# of these applies; this route only knows what each ending does to the item.
_PR_RECONCILE_OUTCOMES = frozenset({"merged", "closed", "abandoned"})

# `merged` and `closed` both return the item to `open` the same way, so they
# share one note keyed by outcome rather than one route each. `abandoned` moves
# nothing (see `_do_reconcile_pr`), so it has none.
_PR_RECONCILE_NOTES = {
    "merged": "PR merged; item returned to open with work left in its body",
    "closed": "PR closed without merging; item returned to open",
}


@app.post("/items/{item_id}/reconcile-pr")
async def reconcile_pr_item(request: Request, item_id: str):
    """End a `pr-open` item's wait on its own linked PR, once the PR itself has
    reached an outcome — the transition `/link-pr` had no way back from
    (`issue_01M2HRVPQN9X8WN3KA0W9S9P30`). An epic whose PR merged with work
    still left in its body used to be unclaimable forever: `_do_release`'s
    `pr-open` branch deliberately does not return the item to `open` while the
    PR is still open, and nothing ever revisited that once the PR wasn't.

    Three outcomes, decided by the caller rather than here:

    - **`"merged"`** — the PR merged and the item's body still lists work.
      Returns the item to `open`.
    - **`"closed"`** — the PR closed without merging. The same return to
      `open`: from this item's side, a closed-unmerged PR and a merged one
      both mean nothing is still open on it.
    - **`"abandoned"`** — the PR is STILL OPEN. This must never return the item
      to `open` on its own — an open PR looks exactly like one still being
      worked, and freeing the item would put a second worker on a live branch.
      It only raises `needs` to `"operator"` so a human sees it; the item
      stays `pr-open` and no state event is written, since none occurred.

    `refs` — `pr` and `url`, set by `/link-pr` — is untouched by every outcome,
    so the PR that ended the wait stays on the row as history.

    **Hold discipline matches `/link-pr` and `/correct`: `_hold_for_write`,
    not a `claimed_by` match.** A `pr-open` item's `claimed_by` is leftover
    text once its own `/release` has already dropped the fence —
    `lease.claim_status` answers `(False, None)` for any state but `claimed`,
    so the field on this row is not a hold at all. Gating on it would refuse
    the one caller this route exists for. What must still be honoured is a
    LIVE lease taken with this item as `root_id`, by a decomposed child's own
    checkout (`issue_01M2HRVPQN9X8WN3KA0W9S9P30`'s comment #2673) — returning
    the item to `open`, or even just flagging it, while that fence stands is
    the failure to avoid, and `_hold_for_write` already refuses every other
    write here on exactly that fence.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "item.claim"):
        return refusals.forbidden("item.claim")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    outcome = payload.get("outcome")
    if not isinstance(outcome, str) or outcome not in _PR_RECONCILE_OUTCOMES:
        return refusals.refuse(
            400, "outcome is not one of the outcomes this route understands",
            supported=sorted(_PR_RECONCILE_OUTCOMES))
    owner, err = _claim_owner(payload, ident)
    if err:
        return err
    try:
        result = await run_in_threadpool(_do_reconcile_pr, ident, item_id,
                                         outcome, owner)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    status, body = result
    return JSONResponse(status_code=status, content=body)


def _refuse_unless_pr_open(item):
    """`/reconcile-pr` moves only an item actually waiting on its own PR.

    The same shapes `_refuse_unless_active` gives for the routes it guards:
    a terminal item says which resolution it already carries, and anything
    else — `open`, `claimed`, or a retired state a row still carries — names
    its own state rather than being moved as if it were `pr-open`.
    """
    if item["state"] == store.ITEM_STATE_PR_OPEN:
        return None
    if item["state"] in store.ITEM_TERMINAL:
        return 409, {"error": f"already {item['state']}"}
    return 409, {"error": f"not pr-open (state {item['state']})"}


def _do_reconcile_pr(ident, item_id: str, outcome: str, owner: str):
    conn = store.connect_rw()
    try:
        item = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                            (item_id,)).fetchone()
        if item is None or not identity.in_scope(ident, item["product"]):
            return 404, {"error": "no such item"}
        refusal = (_refuse_unless_pr_open(item)
                   or _hold_for_write(conn, ident, item, owner,
                                      act="reconciling a PR"))
        if refusal:
            return refusal
        if outcome == "abandoned":
            # **NOT A STATE TRANSITION, so no `_transition` and no event** —
            # the item stays `pr-open`, the same shape `/release`'s `pr-open`
            # branch already established: a write that leaves `state` alone
            # writes nothing to `issue_events`. Guarded by the same `state = ?`
            # predicate every CAS write in this module uses, so a concurrent
            # reopen or resolve landing between the read above and this write
            # loses cleanly (409) instead of being stomped or stomping it.
            cur = conn.execute(
                "UPDATE issue_items SET needs = ? WHERE id = ? AND state = ?",
                (store.ITEM_NEEDS_OPERATOR, item_id, store.ITEM_STATE_PR_OPEN))
            if cur.rowcount != 1:
                conn.rollback()
                fresh = conn.execute(
                    "SELECT state FROM issue_items WHERE id = ?",
                    (item_id,)).fetchone()
                if fresh is None:
                    return 404, {"error": "no such item"}
                return 409, {
                    "error": f"the item is no longer pr-open; it is now "
                             f"{fresh['state']}",
                    "hint": "re-read the item and retry against its current "
                            "state"}
        else:
            # `claimed_by`/`claimed_at` are cleared the same way
            # `lease.release_claim` clears them on a `claimed` release: the
            # item is handed back to the pool, and a reader should not see a
            # stale holder's name on a row that reads as open.
            refusal = _transition(
                conn, item_id, ident.actor, item["state"],
                store.ITEM_STATE_OPEN, _PR_RECONCILE_NOTES[outcome],
                claimed_by=None, claimed_at=None, claimed_by_crew=None)
            if refusal:
                return refusal
        conn.commit()
        fresh = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                             (item_id,)).fetchone()
        return 200, store.item_dict(
            fresh, claim=lease.claim_status(conn, fresh),
            prerequisites=store.prerequisite_status(
                conn, fresh, in_scope=_scope_predicate(ident)))
    finally:
        conn.close()


def _digest_prose(value) -> str | None:
    """A prose field rendered for the audit thread: clipped, with the full length
    and a short digest so two versions sharing a prefix never read as identical.

    A bare prefix misrepresents the change when two versions differ only past the
    cut: the thread shows the same string twice while the stored text moved and
    the approval was revoked (security-reviewer on Orient's PR #163). `None` stays
    `None`, because a field being cleared is a real distinction — and an empty
    string is NOT that distinction (reviewer, PR #11). `""` is a value a caller
    may set and `null` is a clear; rendering both as `None` would have the thread
    report a field cleared when it was set to nothing, which is the same
    misrepresentation the digest exists to prevent one layer over.
    """
    if value is None:
        return None
    if len(value) <= 200:
        return value
    digest = hashlib.sha256(value.encode()).hexdigest()[:8]
    return f"{value[:200]}… ({len(value)} chars, sha256:{digest})"


def _full_sha256(value: str | None) -> str | None:
    """The full sha256 hex digest of a prose field, for a record meant to be
    COMPARED rather than read (`_digest_prose` above is for the latter, and
    truncates). `None` and `""` both record as `None`: a missing field and a
    hash of nothing are different facts, and collapsing them would make "was
    there text" and "does it match" the same question (CQ·146, ADR-0030)."""
    if not value:
        return None
    return hashlib.sha256(value.encode()).hexdigest()


def _validate_prose(payload: dict, field: str):
    """One of ADR-0030's two prose fields off a payload, bounded.

    Returns `(value, refusal)`. `null` clears the field and is not an error, which
    is what an operator sends when a specification turns out to be wrong.
    """
    value = payload.get(field)
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, (400, {
            "error": f"{field} must be a string, or null to clear it"})
    if len(value) > store.MAX_ITEM_TEXT:
        return None, (400, {
            "error": f"{field} must be at most {store.MAX_ITEM_TEXT} characters",
            "length": len(value)})
    return value, None


def _validate_short_description(payload: dict):
    """An item's short label off a payload, stripped and bounded
    (issue_01M0XR0QSMKBSGBYD49CPTJ37E).

    Returns `(value, refusal)`. `null`, and a string that is empty once
    stripped, both clear it. Over `store.MAX_SHORT_DESCRIPTION` is refused with
    the bound named, the same as an over-long `title`, so the caller learns the
    limit from the refusal rather than from a label it never wrote.
    """
    value = payload.get("short_description")
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, (400, {
            "error": "short_description must be a string, or null to clear it"})
    value = value.strip()
    if len(value) > store.MAX_SHORT_DESCRIPTION:
        return None, (400, {
            "error": f"short_description must be at most "
                     f"{store.MAX_SHORT_DESCRIPTION} characters",
            "length": len(value),
            "limit": store.MAX_SHORT_DESCRIPTION})
    return value or None, None


def _validate_topics(payload: dict):
    """An item's grouping labels off a payload, bounded and normalised.

    Returns `(value, refusal)`. `null` and `[]` both clear the topics, which is
    what an operator sends when a grouping turns out to be wrong; they are not
    errors. Order is preserved, duplicates are dropped, and surrounding
    whitespace is stripped, so `["DESIGN", " design "]` cannot store two labels
    that a reader sees as one group.

    **DEDUPE IS CASE-SENSITIVE ON PURPOSE, and the filter matches the same way.**
    Folding case here would make `DESIGN` and `design` one topic on write while
    `topic = ?` still treated them as two on read, which is the disagreement this
    field exists to remove. Whoever wants case-insensitive grouping should change
    both together.
    """
    if "topics" not in payload:
        return None, None
    value = payload["topics"]
    if value is None:
        return [], None
    if not isinstance(value, list):
        return None, (400, {
            "error": "topics must be an array of strings, or null to clear them"})
    if len(value) > store.MAX_TOPICS_PER_ITEM:
        return None, (400, {
            "error": f"at most {store.MAX_TOPICS_PER_ITEM} topics per item",
            "count": len(value)})
    out: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            return None, (400, {"error": "topics must be an array of strings"})
        topic = raw.strip()
        if not topic:
            return None, (400, {
                "error": "a topic must not be empty or whitespace only"})
        if len(topic) > store.MAX_TOPIC:
            return None, (400, {
                "error": f"a topic must be at most {store.MAX_TOPIC} characters",
                "topic": topic[:store.MAX_TOPIC], "length": len(topic)})
        if not set(topic) <= store.KIND_ALLOWED_CHARS:
            # Same refusal a signal kind gets, for the same reason: a confusable
            # renders as an existing topic and matches nothing.
            return None, (400, {
                "error": "a topic must be printable ASCII (0x20-0x7E)",
                "topic": topic})
        if topic not in out:
            out.append(topic)
    return out, None


def _validate_prerequisites(conn, ident, payload: dict, *, item_id,
                            old_prerequisites: list):
    """An item's `prerequisites` off a payload: a full replacement list,
    bounded and validated. Returns `(value, refusal)` where `refusal` is a
    `(status, body)` pair, matching `_validate_parent`'s shape rather than
    `_validate_topics`'s — an item-shaped entry has to be checked against the
    database, so this runs where a connection is already open rather than at
    the route.

    `null` or `[]` clears them, matching `_validate_topics`'s convention.
    `item_id` is `None` on create, where self-reference cannot arise because
    the row does not exist yet — `ref == None` is never true of a submitted
    string, so the check needs no special case for that path.

    **A TEXT ENTRY MATCHING A STORED ONE BY EXACT TEXT KEEPS ITS VERDICT.**
    `old_prerequisites` is the item's own parsed list (`[]` on create, where
    there is nothing to carry forward); a re-sent condition is the same
    condition and a caller replacing the list wholesale must not reset every
    verdict a human already gave just because the call also touched an
    unrelated entry.
    """
    if "prerequisites" not in payload:
        return None, None
    value = payload["prerequisites"]
    if value is None:
        return [], None
    if not isinstance(value, list):
        return None, (400, {
            "error": "prerequisites must be an array of objects, or null to "
                     "clear them"})
    if len(value) > store.MAX_PREREQUISITES_PER_ITEM:
        return None, (400, {
            "error": f"at most {store.MAX_PREREQUISITES_PER_ITEM} "
                     f"prerequisites per item",
            "count": len(value)})
    old_by_text = {e.get("text"): e for e in old_prerequisites
                  if isinstance(e, dict) and "text" in e}
    item_ids: list[str] = []
    seen_items: set[str] = set()
    seen_texts: set[str] = set()
    out: list[dict] = []
    for raw in value:
        if not isinstance(raw, dict) or ("item" in raw) == ("text" in raw):
            return None, (400, {
                "error": "each prerequisite must be an object with exactly "
                         "one of item or text"})
        if "item" in raw:
            ref = raw["item"]
            if not isinstance(ref, str) or not ref.strip():
                return None, (400, {"error": "item must be a non-empty string"})
            ref = ref.strip()
            if ref == item_id:
                return None, (400, {
                    "error": "an item cannot be its own prerequisite"})
            # A DUPLICATE ENTRY SILENTLY KEEPS AN ITEM BLOCKED: the verdict
            # route judges only the first text match by `break`, and a
            # second `{"item": ref}` contributes nothing `blocked` doesn't
            # already get from the first — refused rather than accepted and
            # ignored.
            if ref in seen_items:
                return None, (400, {
                    "error": "prerequisites must not repeat the same item",
                    "item": ref})
            seen_items.add(ref)
            item_ids.append(ref)
            out.append({"item": ref})
        else:
            text = raw["text"]
            if not isinstance(text, str):
                return None, (400, {"error": "text must be a string"})
            text = text.strip()
            if not text:
                return None, (400, {
                    "error": "text must not be empty or whitespace only"})
            if len(text) > store.MAX_PREREQUISITE_TEXT:
                return None, (400, {
                    "error": f"text must be at most "
                             f"{store.MAX_PREREQUISITE_TEXT} characters",
                    "length": len(text)})
            if text in seen_texts:
                return None, (400, {
                    "error": "prerequisites must not repeat the same text",
                    "text": text})
            seen_texts.add(text)
            prior = old_by_text.get(text)
            out.append({
                "text": text,
                "met": prior.get("met") is True if prior else False,
                "judged_by": prior.get("judged_by") if prior else None,
                "judged_at": prior.get("judged_at") if prior else None})
    if item_ids:
        # **ONE WORDING FOR "DOES NOT EXIST" AND "EXISTS, OUT OF SCOPE"**,
        # matching contract §6.1's rule for Course entries — the same reason
        # `_validate_parent` checks scope before naming anything about a
        # parent it found. Splitting the two would let a caller holding one
        # product's token learn which ids exist in a product it cannot read,
        # one item at a time (contract §8).
        placeholders = ",".join("?" * len(item_ids))
        found = {r["id"]: r for r in conn.execute(
            f"SELECT id, product FROM issue_items WHERE id IN ({placeholders})",
            item_ids)}
        unknown = sorted({
            i for i in item_ids
            if i not in found or not identity.in_scope(ident, found[i]["product"])})
        if unknown:
            return None, (400, {"error": "unknown item id(s)", "ids": unknown})
    return out, None


def _spec_gaps(item, *, specification=..., definition_of_done=...) -> list[str]:
    """Which of the two required fields would be empty AFTER this request.

    The sentinels matter: a correction setting `maturity` and nothing else must be
    judged against what the item ALREADY holds, while one setting a specification
    and approving in the same call must be judged against the incoming text.
    Reading the stored value in the second case would refuse a complete request.
    """
    spec = item["specification"] if specification is ... else specification
    dod = item["definition_of_done"] if definition_of_done is ... else definition_of_done
    missing = []
    if not (spec or "").strip():
        missing.append("specification")
    if not (dod or "").strip():
        missing.append("definition_of_done")
    return missing


def _revokes_approval(item, *, effective_type: str | None, spec_changing: bool,
                      end_state_unchanged: bool = False) -> bool:
    """Whether this change revokes an approval the item is carrying.

    **ONE DEFINITION, because three places need the answer and they must agree.**
    `_approval_invariant` decides whether the request is allowed by modelling this
    outcome; `_do_correct` and `_do_revision` produce it. Two of them disagreeing
    means refusing on a state the write never produces, or producing one the gate
    never judged, and the third arrived after the first two had been checked
    against each other by hand (reviewer, PR #11).

    A retype counts wherever it is reachable. `/revision` cannot retype at all, so
    the type half is always false there, and stating it anyway is what keeps the
    two routes one rule rather than two that happen to match today.

    The TEXT half stays epic-scoped, matching the gate it protects: a non-epic may
    be approved with no specification at all, so editing that text invalidates
    nothing. The TYPE half is not scoped, because a retype is what carried an
    approval between those two worlds.

    **`end_state_unchanged` WAIVES THE TEXT HALF AND NEVER THE TYPE HALF, and
    that asymmetry is the whole of its safety.** The operator, 2026-08-27, on ADR-0030's
    stated consequence that fixing a typo un-approves the epic: "I also didn't
    write that and I don't agree with it... we need to distinguish between fixing
    a typo and changing the desired end state." Only the editor knows which one an
    edit was — no comparison of two texts can tell a reflow from a changed end
    state — so the assertion is the caller's and is recorded beside the diff.

    **A retype is not something a caller may assert its way past.** PR #11's
    reviewer found the launder that shape allows: retype an approved epic to
    `improvement` so the text rule stops applying, rewrite the specification,
    retype back. Letting the flag cover a retype rebuilds that door with a
    one-line claim as the key. Retyping an approved item changes what was
    approved whatever its text says, so it revokes unconditionally.
    """
    if item["maturity"] != store.ITEM_MATURITY_APPROVED:
        return False
    if effective_type != item["type"]:
        return True
    return (spec_changing and effective_type == store.ITEM_TYPE_EPIC
            and not end_state_unchanged)


def _approval_invariant(item, *, effective_type: str | None,
                        has_maturity: bool, new_maturity,
                        has_spec: bool, new_spec, has_dod: bool, new_dod,
                        end_state_unchanged: bool = False):
    """ADR-0030's gates, or None when the request passes them.

    **STATED ON THE RESULTING STATE RATHER THAN ON THE TRANSITION.** Orient's
    first version guarded the transition — it fired when `maturity` was being set
    to `approved` — and a reviewer found the hole that shape always has: retyping
    an already-approved improvement to `epic` never touches `maturity`, so nothing
    fired and the result was an approved epic with no specification. A transition
    guard has to enumerate every route into a state; an invariant on the state
    does not.

    **NO CREDENTIAL CHECK IN THIS FUNCTION, and that stays true after
    `epic.approve` (issue_01M32CA70BNV0WEW150V3KYBJ9).** The operator ruled 2026-08-18
    that the Library authorizes and records, and that a restriction on who may
    approve, if one is ever needed, is applied outside — which is now
    `/items/{item_id}/approve`'s own capability and attested-human checks,
    upstream of this call at every one of its three sites. This function still
    only holds the demotion invariant: a consistency rule on the record, shared
    so `/correct`, `/revision` and the approval route cannot disagree about it,
    rather than an authorization check of its own.
    """
    eff_spec = new_spec if has_spec else item["specification"]
    eff_dod = new_dod if has_dod else item["definition_of_done"]
    spec_will_change = ((has_spec and new_spec != item["specification"])
                        or (has_dod and new_dod != item["definition_of_done"]))
    # **A RETYPE REVOKES AN APPROVAL TOO, AND STATING THE INVARIANT PER-CALL WAS
    # NOT ENOUGH TO GET THAT** (reviewer, PR #11). The first version revoked only
    # on a text edit and only while the item was an epic, so three ordinary calls
    # laundered an approval past every gate: retype the approved epic to
    # `improvement` (no text moved, so nothing revoked), rewrite the
    # specification (a non-epic's approval never rested on its text, so the rule
    # is exempt), then retype back to `epic` (no text moved again). The result is
    # an approved epic whose words the operator never saw — the exact state the
    # invariant exists to forbid.
    #
    # **NO CREDENTIAL MADE THAT SEQUENCE WORSE, AND THIS COMMENT USED TO SAY ONE
    # DID.** It read "reached with nobody holding `decide`". There is no `decide`
    # capability here (the operator, 2026-08-18: the Library authorizes and records, and a
    # restriction on who may approve is applied outside), so the aggravating
    # clause named a gate that does not exist and quietly implied the launder
    # would be acceptable from a caller that held it. It would not. The
    # invariant is about what was approved, not about who is asking.
    #
    # The invariant was stated on the resulting state of ONE CALL, and an
    # approval outlives the call. So an approval attaches to what the item IS as
    # well as to what it says: retyping an approved item is a change to the thing
    # that was approved, and the approval does not survive it. That closes the
    # sequence at its first step rather than at the step that looks suspicious,
    # and it holds however many calls a caller strings together, because after
    # step one there is no approval left to launder.
    revoking_change = _revokes_approval(
        item, effective_type=effective_type, spec_changing=spec_will_change,
        end_state_unchanged=end_state_unchanged)
    # **KEEPING `approved` ACROSS A REVOKING CHANGE IS ITSELF AN APPROVAL.**
    #
    # Every other gate keys on the maturity VALUE MOVING, and that is what left
    # the hole Orient's PR #163 closed: resending `approved` moves nothing, so
    # ORIENT'S decide gate skipped, and supplying `maturity` at all suppressed
    # the revocation. That gate was Orient's and is named here as history rather
    # than as something this service has — the Library has no `decide`
    # capability, which is why the demotion below has to win on its own instead
    # of leaning on a gate to refuse the resend. A caller could therefore
    # replace what the operator approved and keep the stamp. Checked here rather
    # than after the epic test below, because a retype to a NON-epic is a
    # revoking change and would otherwise return early — which is the second
    # door onto the same launder.
    #
    # Keeping `approved` across a revoking change does not keep it approved —
    # demotion wins even when the caller resends `maturity: approved` in the
    # same call, since there is no gate left to refuse that resend instead. An
    # approved item may still be retyped or revised freely; it lands on
    # `proposed` where a human looks again either way (ADR-0030).
    if revoking_change and (not has_maturity
                            or new_maturity == store.ITEM_MATURITY_APPROVED):
        effective_maturity = store.ITEM_MATURITY_PROPOSED
    elif has_maturity:
        effective_maturity = new_maturity
    else:
        effective_maturity = item["maturity"]
    if not (effective_type == store.ITEM_TYPE_EPIC
            and effective_maturity == store.ITEM_MATURITY_APPROVED):
        return None
    # AN EPIC CANNOT BE APPROVED WHILE IT SAYS NOTHING ABOUT WHAT IT IS.
    # `approved` is what the Course's unplanned invariant fires on and what
    # ADR-0024 makes the build queue, so approving one with no specification
    # commits us to work nobody has scoped.
    gaps = _spec_gaps(item, specification=eff_spec, definition_of_done=eff_dod)
    if gaps:
        return 400, {
            "error": "an epic cannot be approved while it has no "
                     + " or ".join(gaps),
            "missing": gaps,
            "hint": "approving an epic is approving its specification — the "
                    "operator is agreeing to THIS text, so there has to be text"}
    return None


def _live_course_of(conn, product: str):
    """The one live Course of `product`, or None."""
    return conn.execute(
        "SELECT * FROM courses WHERE product = ? AND status = ?",
        (product, store.COURSE_LIVE)).fetchone()


def _leave_course(conn, item, ident, why: str):
    """Take a demoted epic off the live Course of its product, in the caller's
    transaction. Returns `{"course_id", "position"}` naming where it stood, or
    None when it was on no live Course.

    **ONE TRANSACTION WITH THE DEMOTION, and that is the invariant this exists
    for**: an epic is on the Course exactly while it is approved, so the
    maturity write and this one commit together or neither does. The caller has
    already written `issue_items`, which holds SQLite's write lock, so the read
    below cannot be overtaken by a concurrent reorder.

    **NAMES ITS SUBJECT AND ITS REASON IN THE COURSE'S OWN HISTORY**, because a
    plan that loses an entry with no trace reads as a reorder nobody made. The
    event carries the snapshot for the reason `reordered` does: the array moved.
    """
    course = _live_course_of(conn, item["product"])
    if course is None:
        return None
    entries = store._json_or(course["entries"], [])
    if item["id"] not in entries:
        return None
    index = entries.index(item["id"])
    entries.remove(item["id"])
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(" ")
    conn.execute(
        "UPDATE courses SET entries = ?, version = version + 1, "
        "updated_by = ?, updated_at = ? WHERE id = ?",
        (json.dumps(entries), ident.actor, now, course["id"]))
    conn.execute(
        "INSERT INTO course_events (course_id, actor, action, entries, note, at) "
        "VALUES (?,?,?,?,?,?)",
        (course["id"], ident.actor, store.COURSE_ACTION_DEMOTED,
         json.dumps(entries), f"{item['id']} left the Course: {why}", now))
    return {"course_id": course["id"], "position": index + 1}


def _index_before_demotion(conn, course_id: str, item_id: str,
                           entries: list) -> int | None:
    """Where a re-approved epic stood before its demotion, or None to append.

    The operator, 2026-10-15 (platform/decisions/0087): a re-approval returns the epic
    to the position it left from when its last departure from this Course was
    a demotion, and the Course has changed since only by epics appended at the
    end. Anything else appends, as a first approval does.

    **READ FROM THE SNAPSHOTS, NOT THE NOTES.** Every course event carries the
    order it left behind, so the newest snapshot holding the epic is the order
    just before it left, and the event after that one is the departure. The
    departure must be a demotion that removed this epic and nothing else, and
    today's order must begin with the demotion's snapshot unchanged: a removal
    or reorder of any other epic since moves the neighbours the old index
    named, so it appends instead of guessing.

    **A NULL `entries` IS SKIPPED, NOT READ AS AN EMPTY COURSE.** Retitle,
    supersede and waypoint events write NULL to say the order did not move;
    read as `[]` they look like the epic's departure and suppress the restore
    (PR 286 review, round 1). An unreadable snapshot appends, since it cannot
    say where the epic stood.

    Runs inside the approval's transaction, which already holds the write
    lock, so no reorder can land between this read and the write it informs.
    """
    departure = None
    for row in conn.execute(
            "SELECT action, entries FROM course_events WHERE course_id = ? "
            "ORDER BY id DESC", (course_id,)):
        if row["entries"] is None:
            continue
        try:
            snapshot = json.loads(row["entries"])
        except (TypeError, ValueError):
            return None
        if not isinstance(snapshot, list):
            return None
        if item_id not in snapshot:
            departure = (row["action"], snapshot)
            continue
        if departure is None:
            return None
        action, after = departure
        if action != store.COURSE_ACTION_DEMOTED:
            return None
        if [e for e in snapshot if e != item_id] != after:
            return None
        if entries[:len(after)] != after:
            return None
        return snapshot.index(item_id)
    return None


def _lease_root_visible(conn, ident, held) -> bool:
    """Whether the caller may be told anything about the lease blocking it.

    **THE BLOCKING LEASE'S OWN PRODUCT IS CHECKED, NOT ONLY THE REQUESTED
    ITEM'S.** A caller proves scope over the item it named; the lease blocking it
    sits on a different row, and that row's product is a separate fact.

    **ONE FUNCTION, because this is the third site to need it and the second to
    forget it.** `_lease_refusal` took the check at PR #7, the re-parent blocker
    took it at PR #11, and `/revision`'s holder mismatch shipped without it
    arguing from the upstream invariant in a comment — which is the argument both
    earlier sites already carry a note calling insufficient by itself
    (security-reviewer, PR #12).

    The invariant is real and still holds: `lease.ancestor_ids` is a plain
    `parent_id` walk with no product filter, and every edge is same-product only
    because both writers pass a non-None `product` to `_validate_parent`. An
    import script or a data-fix route that set `parent_id` without it would make
    every one of these sites a live cross-product disclosure at once, which is
    why the check belongs at the disclosure rather than in the argument.
    """
    root = conn.execute("SELECT product FROM issue_items WHERE id = ?",
                        (held["root_id"],)).fetchone()
    return root is not None and identity.in_scope(ident, root["product"])


def _fence_holding(conn, item_id: str, *, extra_roots: list | None = None,
                   ignore_holder: str | None = None, ident=None):
    """The live lease whose fence covers `item_id`, or None.

    A lease on R fences `subtree_ids(R)`, so the leases covering an item are the
    ones rooted at the item itself or at any of its ancestors. `extra_roots` adds
    the position an item is being MOVED to, which has its own ancestor chain.
    `ignore_holder` skips that holder's leases, for a check that only someone
    else's lease may fail. `ident` is the caller presenting that name: a
    crewed lease is skipped only for a caller of the same crew (`lease.held_by`,
    CQ·10), and a call that names a holder without `ident` skips no crewed
    lease at all.

    A DEAD LEASE DOES NOT COUNT. It is the row occupying the root until someone
    reclaims it, and its holder is by definition not watching, so blocking a
    re-parent on it would let one abandoned session freeze the shape of the tree
    until its grace period ran out.
    """
    roots = [item_id] + list(lease.ancestor_ids(conn, item_id))
    for extra in extra_roots or []:
        roots += [extra] + list(lease.ancestor_ids(conn, extra))
    for root in dict.fromkeys(roots):
        row = lease.live_lease_on(conn, root)
        if (row is not None and not lease.is_dead(row)
                and not lease.held_by(row, ignore_holder, **_crew_of(ident))):
            return row
    return None


def _crew_of(ident) -> dict:
    """The `crew`/`crew_exempt` pair `lease.held_by` judges a holder match
    with, for `ident` (None when no caller is named)."""
    if ident is None:
        return {"crew": None, "crew_exempt": False}
    return {"crew": ident.crew, "crew_exempt": ident.kind == "approved_client"}


def _hold_for_write(conn, ident, item, owner: str, *, act: str):
    """None when this caller may change `item` now, or the `(status, body)` refusal.

    **ONLY A CHECKED-OUT ITEM CHANGES, AND NEVER UNDER SOMEONE ELSE'S CHECKOUT**
    (the operator, 2026-09-11: "Only checked out items can be changed", because "it would
    be disruptive to close or change an item that has been checked out by someone
    else"). Three cases:

    - the live lease covering the item is the caller's: the write goes ahead
      under it;
    - no live lease covers it: the caller holds it for this one write, recorded
      by `lease.record_write` as a lease taken and released inside the write's
      own transaction. The operator chose this over requiring a separate checkout call,
      because a leaf's checkout fences its whole parent epic, and a triage pass
      making hundreds of changes would fence each epic for as long as it took
      to decide;
    - another holder's live lease covers it: refused 409 with `reason: "held"`,
      which a sweep reads as "someone is working on this, skip it" rather than
      as a failure.

    **IT TAKES THE WRITE LOCK BEFORE LOOKING**, so a checkout cannot commit
    between this check and the caller's write. Every caller reaches here before
    its first write, and the lock is held until the caller commits or closes.

    **A LIVE LEASE RECORDED AS A DIFFERENT CREW'S REFUSES BEFORE THE `owner`
    STRING IS EVEN COMPARED** (CQ·10). `held["holder"] == owner` passes for
    an attacker who read the real holder's name off the item or the lease
    and typed it back — that is the whole defect this ruling closes — so the
    crew check has to run first, not as a second reason alongside it.
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    held = _fence_holding(conn, item["id"])
    if held is None:
        lease.record_write(conn, item, holder=owner, actor=ident.actor, act=act,
                           holder_crew=ident.crew)
        return None
    if _crew_mismatch(ident, _row_crew(held, "holder_crew")):
        return 409, _crew_mismatch_refusal()
    if held["holder"] == owner:
        return None
    if not _lease_root_visible(conn, ident, held):
        return 409, {
            "error": f"{item['id']} is held by another session, and that lease "
                     f"is outside this identity's scope",
            "reason": "held",
            "hint": "the hold is real but its holder is not yours to see; leave "
                    "the item to it"}
    return 409, {
        "error": f"{item['id']} is held by {held['holder']}, not {owner}; "
                 f"{act} waits for the holder",
        "reason": "held",
        "lease": lease.lease_dict(held, conn=conn),
        "hint": "if this checkout is yours, pass the owner you checked out as; "
                "otherwise leave the item to its holder"}


APPROVE_ROUTE = "/items/{item_id}/approve"
MODEL_ROUTE = "/items/{item_id}/model"


@app.post("/items/{item_id}/correct")
async def correct_item(request: Request, item_id: str):
    """Correct a misfiled field: `type`, `severity`, `parent_id`,
    `found_during`, `topics`, `prerequisites`, `specification` or
    `definition_of_done`.

    **`maturity` IS REFUSED HERE, AND BY NAME.** Approving an epic is
    `POST /items/{item_id}/approve`, gated on a capability only an operator
    surface's credential holds, and the route this replaces let any dispatched
    agent set `approved` on the strength of `item.resolve`. The refusal covers
    every value, `null` included, so this route is not a second way to move the
    field. Every other field is corrected as before; what an edit does to an
    approval it finds (`_approval_invariant`) is unchanged.

    **THIS IS A CORRECTION PATH, NOT A GENERAL EDIT API.** `title`, `body`,
    `state` and `needs` are not editable here; `/revision` is the route for an
    item's content, and it takes the lease as its authorization. **`product`
    changes only through `POST /items/{item_id}/product`, which moves the whole
    subtree**, and the descendant-count disclosure below depends on that staying
    true.

    **`topics` IS HERE RATHER THAN ON `/revision`, AND THE LINE IS CONTENT.** A
    topic is a classification — which area of a product this item belongs to —
    not something authored about the work. That is the same line `type` and
    `severity` already sit on. `null` or `[]` clears them.

    **`type` accepts only `epic` or `null`.** A work item carries no type
    (issue_01M0EEPS591VQHYEBJY7NGF4C3), so `null` retypes to untyped and `epic`
    promotes; `bug`, `nit`, `improvement`, `task` and `feature` are refused by
    name. Retyping an epic with children, or one that is approved, follows the
    same rules either direction already enforced below.

    **A correction needs the item held, and never by someone else**
    (`_hold_for_write`, the operator 2026-09-11). Pass `owner` if you checked it out;
    with no live lease on it, the route holds it for this one write, so
    regrouping a queue still needs no separate checkout call.

    `parent_id` is here for a different reason from the other fields. Those
    correct a MISFILING; this one records a fact that legitimately becomes known
    long after filing — an epic usually does not exist when the work under it is
    first captured. On a work item, an explicit `null` or `""` reverses the
    nesting into the item's product's corrections queue
    (`library/specs/one-home-per-work-item`), since a wrong nesting that could
    not be undone would be a one-way door; on an epic, `null` clears the parent
    to top level, as it always has. **A move changes `parent_id` alone** (the
    operator's ruling, Slate a-v5j93k, 2026-10-09): it writes no note, no
    comment and no event, whether the caller asked for it directly or a retype
    carried it along — unlike every other field this route corrects.

    **`found_during` records which epic's work turned this item up, without
    the lane change `parent_id` carries.** It must name an existing epic in
    the item's own product (`_validate_found_during`); every refusal past a
    bare type check reads "unknown item id(s)", the same wording
    `prerequisites` gives an id it cannot resolve, so this field is no
    existence oracle. `null` clears it. Setting it changes neither
    `parent_id` nor the corrections lane nor any epic's own approval.

    **Re-parenting is refused while ANOTHER holder's live lease covers the item**,
    in either its current position or its destination. The caller's own leases
    never refuse it (the operator, 2026-09-11: "The idea is that *only* the person/agent
    with the checkout can make the change. Not that *nobody* can make a
    change."). A lease protects the tree's shape from outsiders, and the holder
    moving an item inside fences it holds is not one. Where a move touches two
    fences, the caller must hold each of them or face no other holder on it.

    **There is no `refs` door here, deliberately.** A correction route that
    accepted `refs` is a second place any future controlled key would have to
    be remembered to strip, on top of create and resolve — and that was
    already dropped once (`issue_01KZMNC9RASH1JH9NTYGB2WW0M`, when `refs` was
    still `decision_id`'s only door in). Omitting the field here is what makes
    a future control unforgettable rather than merely documented.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "item.resolve"):
        return refusals.forbidden("item.resolve")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    refused = _refuse_maturity(payload) or _refuse_model_choice(payload)
    if refused:
        return refused

    # PRESENCE, NOT TRUTHINESS, for the fields an explicit `null` means
    # something on. `payload.get()` cannot tell a null from an absent key, and
    # null is how a caller resets maturity to "not yet assessed", moves a work
    # item to its product's corrections queue (or an epic to top level),
    # clears which epic's work found it, or clears a specification that
    # turned out to be wrong.
    # `type` joins the presence-tracked fields here: `null` is now a real
    # target (untyped), not a no-op, so "was `type` sent at all" has to be
    # asked rather than "is the value non-null".
    fields = {name: name in payload for name in
              ("type", "parent_id", "found_during", "specification",
               "definition_of_done", "topics", "prerequisites")}
    new_type = payload.get("type")
    new_severity = payload.get("severity")
    if new_severity is None and not any(fields.values()):
        return refusals.refuse(
            400, "at least one of type, severity, parent_id, found_during, "
                 "topics, prerequisites, specification or definition_of_done "
                 "is required")
    # Both validated unconditionally, so neither name is bound only on the other
    # one's success — the earlier shape was one intervening line away from an
    # UnboundLocalError for whoever edited it next (reviewer, PR #11).
    new_spec, spec_err = _validate_prose(payload, "specification")
    new_dod, dod_err = _validate_prose(payload, "definition_of_done")
    new_topics, topics_err = _validate_topics(payload)
    err = spec_err or dod_err or topics_err
    if err:
        return JSONResponse(status_code=err[0], content=err[1])
    if new_type is not None:
        if not isinstance(new_type, str) or new_type.strip() not in store.ITEM_TYPES:
            return refusals.refuse(400, f"type {new_type!r} is not a known type",
                                   supported=sorted(store.ITEM_TYPES))
        new_type = new_type.strip()
    if new_severity is not None:
        if (not isinstance(new_severity, str)
                or new_severity.strip() not in store.ITEM_SEVERITIES):
            return refusals.refuse(
                400, f"severity {new_severity!r} is not a known severity",
                supported=sorted(store.ITEM_SEVERITIES))
        new_severity = new_severity.strip()
    # Parsed here; the conflict with a retype is decided in `_do_correct`, where
    # the item's own type is known and the comparison can be the same one the
    # invariant makes.
    unchanged, err = _end_state_unchanged(payload)
    if err:
        return err

    owner, err = _claim_owner(payload, ident)
    if err:
        return err

    try:
        result = await run_in_threadpool(
            functools.partial(
                _do_correct, ident, item_id, payload,
                {"type": new_type, "severity": new_severity,
                 "specification": new_spec,
                 "definition_of_done": new_dod, "topics": new_topics}, fields,
                end_state_unchanged=unchanged, owner=owner))
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    status, body = result
    return JSONResponse(status_code=status, content=body)


def _refuse_maturity(payload: dict):
    """The refusal for a request that names `maturity`, or None.

    **PRESENCE, NOT VALUE, AND ON EVERY ROUTE THAT ONCE WROTE THE FIELD.**
    `/correct` and `/revision` both did, so refusing on one would have left the
    other as the door. A value test (`approved` only) would leave `null`,
    `built` and `superseded` settable by any agent credential and would need
    revisiting whenever the ladder grew; the specification says a request that
    sets `maturity` is refused. The refusal names the approval route, which is
    what a caller holding the wrong route needs to be told.
    """
    if "maturity" not in payload:
        return None
    return refusals.refuse(
        400, "maturity is not written by this route; an epic is approved "
             "through POST /items/{item_id}/approve",
        hint="the approval route takes the acting_human the client acted for "
             "and needs a credential holding epic.approve; every other field "
             "this route accepts is unchanged, so resend the request without "
             "`maturity`",
        route=f"POST {APPROVE_ROUTE}")


def _refuse_model_choice(payload: dict):
    """The refusal for a request that names `model_choice`, or None.

    Presence, not value, for `_refuse_maturity`'s reason: `/correct`,
    `/revision` and `POST /items` are agent-writable, and a model an agent could
    put on an item would remove the roster's model pin from the overnight run.
    The refusal names the one route that writes it.
    """
    if "model_choice" not in payload:
        return None
    return refusals.refuse(
        400, "model_choice is not written by this route; an operator sets it "
             "through POST /items/{item_id}/model",
        hint="that route needs a credential holding item.model and the "
             "acting_human the client acted for; every other field this route "
             "accepts is unchanged, so resend the request without "
             "`model_choice`",
        route=f"POST {MODEL_ROUTE}")


def _left_course_note(left) -> str:
    """The thread line for a demotion, saying whether the epic left a Course."""
    if left is None:
        return "\n\nIt was on no live Course, so none was changed."
    return (f"\n\n**Left the Course.** Removed from {left['course_id']}, where it "
            f"stood at position {left['position']}; approval is what puts an "
            f"epic on a Course, so a demotion takes it off.")


def _end_state_unchanged(payload: dict):
    """The caller's assertion that this edit does not move the desired end state,
    parsed and validated. **The conflict with a retype is NOT decided here** —
    see `_retype_conflict`, which is called where the item's own type is known.

    **THE DEFAULT IS THAT AN EDIT UN-APPROVES, and the caller opts out on the
    call rather than opting in.** Defaulting the other way lets an end-state
    change keep an approval in silence, which is the failure worth avoiding; this
    way the quiet path is the safe one and a claim is something someone made.

    **A RETYPE IS REFUSED RATHER THAN WAIVED, and that is not tidiness.** PR #11's
    reviewer found the launder a text-only rule allows: retype an approved epic to
    `improvement` so the text rule stops applying, rewrite the specification,
    retype back. A flag that covered a retype would rebuild that door with a
    one-line claim as its key. Refusing names the misunderstanding at the call
    site instead of letting a caller believe the assertion covered more than it
    does.
    """
    if "end_state_unchanged" not in payload:
        return False, None
    value = payload["end_state_unchanged"]
    if not isinstance(value, bool):
        return None, refusals.refuse(
            400, "end_state_unchanged must be true or false",
            hint="it asserts that this edit leaves the desired end state as it "
                 "was, so an approval the item carries survives it")
    return value, None


def _retype_conflict(*, end_state_unchanged: bool, item, effective_type: str | None):
    """The refusal when the assertion is sent alongside a real type change, or
    None.

    **KEYED ON `effective_type != item["type"]`, WHICH IS THE CONDITION
    `_revokes_approval` USES, AND THAT AGREEMENT IS THE WHOLE POINT** (reviewer,
    PR #67, round 3). The check used to run at the route on `new_type is not
    None` — the PRESENCE of a `type` field rather than a change to it — because
    the item was not loaded there yet. Two conditions for one concept, and they
    disagreed exactly where a client sends every field back including an
    unchanged `type`, which is what a full-object PATCH does. Such a caller could
    never use the assertion at all, and the revocation note cheerfully told it to.
    
    **The strictness bought no safety, which is why closing the gap is not a
    relaxation of the launder guard.** A `type` equal to the item's own cannot
    launder anything: `_revokes_approval` returns False for it and True for a
    real retype, on this same comparison. The guard is against changing what the
    item IS, and a no-op change does not.

    So this runs where the item is loaded and `effective_type` is already
    computed, next to the invariant it has to agree with, rather than one
    function earlier on a value that only resembled it.
    """
    if not end_state_unchanged or effective_type == item["type"]:
        return None
    # **THE `(status, body)` TUPLE EVERY OTHER REFUSAL IN `_do_correct` RETURNS**,
    # rather than a `JSONResponse` the caller decodes back (reviewer, PR #67).
    # `_approval_invariant` and the parent, fence and epic-children checks all
    # return this shape; building a response here meant `_do_correct` encoding to
    # JSON and immediately parsing it again to fit.
    return 400, {
        "error": "end_state_unchanged cannot be asserted alongside a type change",
        "hint": "retyping an item changes what was approved whatever its text "
                "says, so the approval does not survive it either way",
        "current_type": item["type"], "requested_type": effective_type}


def _do_correct(ident, item_id: str, payload: dict, new: dict, fields: dict,
                *, end_state_unchanged: bool = False, owner: str):
    """Returns `(status, body)`."""
    conn = store.connect_rw()
    try:
        # **THE WRITE LOCK IS TAKEN BEFORE THE READ** (CQ·100). Every gate
        # below, the approval invariant above all, judges this row. Read
        # before `_hold_for_write`'s `BEGIN IMMEDIATE`, a concurrent
        # correction to a different column could commit in between, and both
        # would decide whether their edit revokes approval against the same
        # stale row; both UPDATEs land, since they touch different columns,
        # and `approved` stands over a combination nobody judged.
        conn.execute("BEGIN IMMEDIATE")
        item = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                            (item_id,)).fetchone()
        if item is None or not identity.in_scope(ident, item["product"]):
            return 404, {"error": "no such item"}
        if item["state"] in store.ITEM_TERMINAL:
            return 409, {"error": f"item is {item['state']}; corrections are "
                                  f"not allowed on terminal items"}
        refusal = _hold_for_write(conn, ident, item, owner, act="a correction")
        if refusal:
            return refusal[0], refusal[1]
        new_type = new["type"]
        # RETYPING AN EPIC THAT STILL HAS CHILDREN CORRUPTS THE TREE. ADR-0023's
        # invariant is that the tree's interior is all epics and its leaves are
        # all items, and every traversal is written assuming it. `_validate_parent`
        # refuses a NEW child under a non-epic but cannot see children that
        # already exist, so retyping the parent leaves them pointing at an
        # interior node that is no longer an epic — and the lease would fence a
        # subtree hanging off something that cannot hold one.
        #
        # Refuse rather than cascade: re-parenting or resolving the children is a
        # decision about the work, not a side effect a correction takes on the
        # caller's behalf.
        #
        # **`fields["type"]`, NOT `new_type is not None`.** `null` is now a real
        # requested type — untyped — so a presence check is what tells "no type
        # change requested" from "retype to untyped requested"; the old value
        # check read the second as the first and skipped this gate.
        if (fields["type"] and new_type != item["type"]
                and item["type"] == store.ITEM_TYPE_EPIC):
            try:
                kids = [k for k in lease.subtree_ids(conn, item_id)
                        if k != item_id]
            except lease.SubtreeTooLarge as too_big:
                # The tree could not be enumerated, so whether this epic has
                # children is unknown — and "unknown" is not "none". Refusing
                # matches what checkout does when the same walk gives out: the
                # alternative is retyping an epic on the strength of a count
                # nobody obtained (security-reviewer, PR #11).
                return 409, {
                    "error": f"cannot enumerate this subtree: {too_big}",
                    "hint": "the tree beneath this item is malformed or far "
                            "larger than expected; a retype cannot be judged "
                            "safe until it can be counted"}
            if kids:
                # THE COUNT IS SAFE TO DISCLOSE because every descendant inherits
                # its epic's product: `product` changes only by moving a whole
                # subtree (`move_item_product`), and every edge is
                # same-product, at create and at re-parent alike, because both go
                # through `_validate_parent` with a non-None `product`. A subtree
                # cannot span products, so the number reveals nothing a caller
                # who can see the epic could not already read.
                return 409, {
                    "error": f"cannot retype an epic with {len(kids)} "
                             f"descendant(s)",
                    "hint": "re-parent or resolve the children first"}
        # THE APPROVAL INVARIANT FIRES BEFORE ANY MUTATION, and the position is
        # load-bearing rather than tidy: a refusal placed after the assignments
        # would answer 403 having already staged the change, and the caller would
        # read "forbidden" while part of the request had happened.
        effective_type = new_type if fields["type"] else item["type"]
        # **BEFORE THE INVARIANT AND BEFORE ANY MUTATION**, and on the same
        # comparison the invariant makes — see `_retype_conflict`. Placed here
        # rather than at the route because this is the first point at which the
        # item's own type is known, and the requested type only resembled it.
        conflict = _retype_conflict(end_state_unchanged=end_state_unchanged,
                                    item=item, effective_type=effective_type)
        if conflict is not None:
            return conflict[0], conflict[1]
        err = _approval_invariant(
            item, effective_type=effective_type,
            has_maturity=False, new_maturity=None,
            has_spec=fields["specification"], new_spec=new["specification"],
            has_dod=fields["definition_of_done"],
            new_dod=new["definition_of_done"],
            end_state_unchanged=end_state_unchanged)
        if err:
            return err[0], err[1]

        new_parent = item["parent_id"]
        if fields["parent_id"]:
            # `item["product"]` IS WHAT MAKES THE SAME-PRODUCT CHECK MEAN
            # ANYTHING HERE. The check itself is unconditional since PR #12, so a
            # None no longer skips it and would instead refuse every parent; the
            # escape hatch that used to fail open is gone. What the item's own
            # product buys is the check being asked the right question, and the
            # edge it enforces is why the descendant-count disclosure above stays
            # safe. Passing `item_id` is what arms the cycle check, which matters
            # far more here than at create: a fresh item has no descendants to
            # close a loop through, and an existing one does.
            new_parent, perr = _validate_parent(
                conn, payload.get("parent_id"), ident=ident,
                product=item["product"], item_id=item_id,
                item_type=effective_type)
            if perr:
                return perr[0], perr[1]
        elif (effective_type == store.ITEM_TYPE_EPIC
              and item["type"] != store.ITEM_TYPE_EPIC
              and item["parent_id"] is not None
              and store.get_corrections_queue(conn, item["parent_id"])
              is not None):
            # A retype to epic that leaves the parent alone keeps the stored
            # one, so `_validate_parent` never sees it. A queue is the one
            # parent a non-epic may hold that an epic may not, so moving an
            # item into its queue and then retyping it in a second request
            # would store an epic under a queue (reviewer, PR #237 round 1).
            return 400, {
                "error": "an epic's parent must be an epic or nothing",
                "hint": "this item's parent is a corrections queue; send "
                        "parent_id null or an epic id with the retype"}
        # **A WORK ITEM'S PARENT IS NEVER NULL** (`library/specs/one-home-per-
        # work-item`, PR 2). This covers both ways a non-epic can end up here
        # with no parent: an explicit `null`/`""` from the caller
        # (`fields["parent_id"]`), already resolved to `None` by
        # `_validate_parent` above, and a parentless epic retyped down to a
        # work item with no `parent_id` in the request at all — caught on
        # `item["type"]` still reading `epic` here, since `new_parent` is
        # left at its untouched starting value of `item["parent_id"]` in
        # that case. **Gated on one of those two, not on `new_parent is
        # None` alone** — a correction that touches neither, on a non-epic
        # row a NULL parent reached some other way (a direct insert outside
        # `insert_item`; see `GET /items?home=invalid`), must not mint a
        # queue assignment nobody asked this request for. Creating the queue
        # here, inside `_hold_for_write`'s own `BEGIN IMMEDIATE`, matches
        # `insert_item`'s reasoning for doing the same at create. A non-epic
        # whose inherited parent already names an epic (a sub-epic retyped
        # to a work item) is untouched: that parent is already a valid home
        # for a work item.
        if (effective_type != store.ITEM_TYPE_EPIC and new_parent is None
                and (fields["parent_id"]
                     or item["type"] == store.ITEM_TYPE_EPIC)):
            new_parent = store.get_or_create_corrections_queue(
                conn, item["product"])
        if fields["parent_id"] and new_parent != item["parent_id"]:
            # MOVING AN EDGE UNDER SOMEONE ELSE'S LIVE LEASE CHANGES WHAT THAT
            # FENCE HOLDS WITHOUT ITS HOLDER KNOWING. Moving an item out shrinks
            # the fence its holder is working behind; moving it in silently pulls
            # it into someone else's. BOTH POSITIONS ARE CHECKED: an item leaving
            # a fence is the case that looks harmless and is not.
            #
            # **THE CALLER'S OWN LEASES ARE SKIPPED** (the operator, 2026-09-11: "*only*
            # the person/agent with the checkout can make the change. Not that
            # *nobody* can make a change"). This refused the holder too, until
            # then, so moving an item meant releasing its checkout first and
            # leaving it unprotected for exactly the change that most needed it.
            # A holder nesting one fence it holds inside another leaves two of
            # its own leases overlapping. That blocks no one else, so the move
            # goes through. Its one cost falls on the holder: `lease.take` does
            # not compare holders, so it will refuse the holder a later checkout
            # of a sub-epic inside the inner fence, ground it already covers.
            # What is refused is a move that carries ANOTHER holder's lease into
            # a fence; that check follows this one.
            #
            # **THE COLLISION-GUARD CAVEAT HOLDS HERE TOO, AT BOTH ENDS OF THE
            # MOVE** (security-reviewer, PR #108 round 2). `owner` is
            # self-declared, so a caller naming the destination's holder passes
            # this check as that holder would, the same way it passes
            # `_hold_for_write` for the item and checkout for an epic (contract
            # §4: "The lease is a collision guard, not an isolation boundary").
            # Letting the holder through, which the operator's rule requires, and refusing
            # someone who claims the holder's name cannot both be done with a
            # label. It needs Phase 5's per-session token, which makes `holder`
            # verifiable. What this check does stop is the honest mistake: a
            # session moving an item into or out of a fence it does not hold.
            # Since CQ·10 a crewed lease also needs the caller's crew to match
            # (`lease.held_by`), so the name alone no longer passes across crews.
            blocker = _fence_holding(
                conn, item_id, extra_roots=[new_parent] if new_parent else [],
                ignore_holder=owner, ident=ident)
            if blocker is not None:
                # **THE BLOCKING LEASE'S OWN PRODUCT IS CHECKED HERE, NOT ONLY
                # UPSTREAM** (security-reviewer, PR #11). `_lease_refusal` carries
                # a comment saying its `visible` branch is unreachable "since no
                # route can create a cross-product parent — which is exactly the
                # argument that ages badly. The re-parent surface is already
                # reserved in the contract." This route IS that surface, so it
                # takes the check rather than inheriting the argument.
                if not _lease_root_visible(conn, ident, blocker):
                    return 409, {
                        "error": "another holder's live lease covers this item "
                                 "where it is or where it would go, and it is "
                                 "outside this identity's scope",
                        "reason": "held",
                        "hint": "the fence is real but its holder is not yours "
                                "to see; wait for it to release"}
                return 409, {
                    "error": "another holder's live lease covers this item "
                             "where it is or where it would go; re-parenting "
                             "would change what that fence holds without "
                             "telling its holder",
                    "reason": "held",
                    "lease": lease.lease_dict(blocker, conn=conn),
                    "hint": "wait for the holder to release, or have a human "
                            "override the lease first"}
            # **A MOVE INTO A FENCE MUST NOT CARRY SOMEONE ELSE'S LEASE WITH IT**
            # (reviewer, PR #108 round 2). The check above looks up from the
            # item; this one looks down. An epic nobody holds can contain a
            # sub-epic another session holds, and moving it under an epic the
            # caller holds would nest that session's fence inside the caller's.
            # Only a fenced destination can create that overlap, and after the
            # check above any fence there is the caller's, so an unfenced
            # destination skips the walk.
            if new_parent and _fence_holding(conn, new_parent) is not None:
                try:
                    beneath = [c.lease for c in lease.live_conflicts(conn, item_id)
                               if c.blocks_by == "descendant"
                               and not lease.held_by(c.lease, owner,
                                                     **_crew_of(ident))]
                except lease.SubtreeTooLarge as too_big:
                    return 409, {
                        "error": f"cannot enumerate this subtree: {too_big}",
                        "hint": "the tree beneath this item is malformed or far "
                                "larger than expected; a move into a fence "
                                "cannot be judged without it"}
                if beneath:
                    inner = beneath[0]
                    if not _lease_root_visible(conn, ident, inner):
                        return 409, {
                            "error": "something beneath this item is held by "
                                     "another session outside this identity's "
                                     "scope, and the move would put it inside "
                                     "your fence",
                            "reason": "held",
                            "hint": "the hold is real but its holder is not "
                                    "yours to see; move the item somewhere no "
                                    "one holds"}
                    return 409, {
                        "error": f"{inner['root_id']} beneath this item is held "
                                 f"by {inner['holder']}; moving it under "
                                 f"{new_parent} would put that fence inside "
                                 f"yours",
                        "reason": "held",
                        "lease": lease.lease_dict(inner, conn=conn),
                        "hint": "wait for that holder to release, or move the "
                                "item somewhere no one holds"}

        new_found_during = item["found_during"]
        if fields["found_during"]:
            new_found_during, fd_err = _validate_found_during(
                conn, payload.get("found_during"), ident=ident,
                product=item["product"], item_id=item_id)
            if fd_err:
                return fd_err[0], fd_err[1]

        # **SEQUENCING, NOT THE SPECIFICATION THE OPERATOR APPROVED — DELIBERATELY NOT
        # THREADED THROUGH `_approval_invariant` OR `_revokes_approval`.**
        # Those two gate the text an operator agreed to; a prerequisite says
        # WHEN an item may be worked, which an approval never spoke to. Wiring
        # this in would demote an approved epic every time its prerequisites
        # were merely reordered.
        new_prereqs = None
        if fields["prerequisites"]:
            new_prereqs, prereq_err = _validate_prerequisites(
                conn, ident, payload, item_id=item_id,
                old_prerequisites=store._prerequisite_entries(item["prerequisites"]))
            if prereq_err:
                return prereq_err[0], prereq_err[1]

        changes, columns = [], {}
        # **`parent_id` GATES THE WRITE BUT NEVER JOINS THE NOTE** (the
        # operator's ruling, Slate a-v5j93k, 2026-10-09). Every other field
        # below feeds both `columns`, which decides whether the UPDATE runs,
        # and `changes`, which decides whether a "Field correction" note is
        # written — the two were one list until this PR. A re-parent is the
        # one correction with no note, no comment and no event, so it is
        # split out here rather than folded into the loop just below: it
        # still has to reach `columns` so a parent-only request is not
        # silently dropped at the `if not columns` gate further down, and
        # `new_parent` already reflects both an explicit move and a retype
        # that carried one along (see the block above that resolves it).
        if new_parent != item["parent_id"]:
            columns["parent_id"] = new_parent
        if new["severity"] is not None and new["severity"] != item["severity"]:
            changes.append(("severity", item["severity"], new["severity"]))
            columns["severity"] = new["severity"]
        # Presence-gated, so an explicit null on an assessed item records a real
        # change back to "not yet assessed" instead of reading as an omitted
        # field and being silently dropped. `type` sits here rather than beside
        # `severity` above for the same reason: `null` retypes to untyped, a
        # real target rather than an omitted field.
        for name, value in (("type", new_type),
                            ("found_during", new_found_during),
                            ("specification", new["specification"]),
                            ("definition_of_done", new["definition_of_done"])):
            if fields[name] and value != item[name]:
                changes.append((name, item[name], value))
                columns[name] = value
        # **TOPICS COMPARE AS LISTS AND STORE AS JSON, so this cannot join the
        # loop above.** `item["topics"]` is the raw column — JSON text, or NULL
        # on every row filed before anything wrote it — while the validated value
        # is a list. Comparing those two directly reports a change on every call,
        # which would write an audit entry and bump the item for a request that
        # altered nothing.
        if fields["topics"]:
            old_topics = store._json_or(item["topics"], [])
            if new["topics"] != old_topics:
                changes.append(("topics", old_topics, new["topics"]))
                columns["topics"] = json.dumps(new["topics"])
        # SAME SHAPE AS `topics` JUST ABOVE, for the same reason: the stored
        # column is JSON text and the validated value is a list, so comparing
        # them directly would report a change — and write an audit entry — on
        # every call that merely resent the field unchanged.
        if fields["prerequisites"]:
            old_prereqs = store._json_or(item["prerequisites"], [])
            if new_prereqs != old_prereqs:
                changes.append(("prerequisites", old_prereqs, new_prereqs))
                columns["prerequisites"] = json.dumps(new_prereqs)
        spec_edited = any(f in columns
                          for f in ("specification", "definition_of_done"))
        # CHANGING WHAT WAS APPROVED RETURNS THE ITEM TO `proposed` (ADR-0030).
        # An approval attaches to the text AND to the type, so either moving
        # afterwards leaves `approved` standing over something nobody agreed to —
        # silently, which is the worst property a gate can have. **Through the
        # SAME helper `_approval_invariant` used**: the gate above decides whether
        # the request is allowed by modelling this outcome, so the two disagreeing
        # would mean refusing on a state the write never produced, or producing
        # one the gate never judged.
        #
        # This route never sets `maturity` (`_refuse_maturity`, above), so the
        # value standing after this edit is always the item's own, and that is
        # what `_revokes_approval` judges, matching what `_approval_invariant`
        # already modelled above.
        final_maturity = item["maturity"]
        unapproved_by_edit = (
            final_maturity == store.ITEM_MATURITY_APPROVED
            and _revokes_approval(item, effective_type=effective_type,
                                  spec_changing=spec_edited,
                                  end_state_unchanged=end_state_unchanged))
        # **THE NOTE FIRES WHEN THE ASSERTION ACTUALLY KEPT SOMETHING, AND THAT
        # IS ASKED OF `_revokes_approval` RATHER THAN RESTATED** (reviewer and
        # security-reviewer, PR #65, independently). Re-listing the clauses here
        # is how the note drifts from the rule it describes; asking the shared
        # definition what it WOULD have answered without the assertion cannot.
        #
        # **The record is the only safety this mechanism has, so it must not be
        # able to lie.** Nothing verifies the assertion; what is offered instead
        # is an attributed note. Two ordinary calls wrote "the approval it
        # already carried still stands" when nothing was kept: a spec edit on a
        # `proposed` item, which carried no approval, and a spec edit on an
        # approved NON-epic, whose approval never rested on that text and was
        # never at risk. Both read identically to a true note, which is the
        # look-alike shape the flag itself exists to avoid, and a client sending
        # the flag defensively on every spec edit would trip them routinely.
        kept_by_assertion = (
            end_state_unchanged
            and final_maturity == store.ITEM_MATURITY_APPROVED
            and _revokes_approval(item, effective_type=effective_type,
                                  spec_changing=spec_edited,
                                  end_state_unchanged=False))
        if unapproved_by_edit:
            changes.append(("maturity", item["maturity"],
                            store.ITEM_MATURITY_PROPOSED))
            columns["maturity"] = store.ITEM_MATURITY_PROPOSED
        # **GATED ON `columns`, NOT `changes`** — the two diverge now that a
        # re-parent can populate `columns` alone. A request that changed only
        # `parent_id` must still run the UPDATE below; gating on `changes`
        # would read it as a no-op and silently drop the move.
        if not columns:
            return 200, store.item_dict(
                item, claim=lease.claim_status(conn, item),
                prerequisites=store.prerequisite_status(
                    conn, item, in_scope=_scope_predicate(ident)))

        # **CONDITIONAL ON THE STATE THIS REQUEST READ, the same compare-and-swap
        # checkout/resolve/link-pr all carry** (security-reviewer,
        # PR #81, round 2). The SELECT above and every gate since take no lock, so
        # a concurrent `/resolve` that commits in this window used to
        # be silently overwritten by this UPDATE — landing a correction on an
        # item that had already gone terminal, contradicting the refusal this
        # same function gives when it reads that state directly. The loser rolls
        # back and is told the state it collided with, the same as `_transition`.
        prev = item["state"]
        sets = ", ".join(f"{c} = ?" for c in columns)
        cur = conn.execute(f"UPDATE issue_items SET {sets} WHERE id = ? "
                           f"AND state = ?",
                           [*columns.values(), item_id, prev])
        if cur.rowcount != 1:
            conn.rollback()
            fresh = conn.execute("SELECT state FROM issue_items WHERE id = ?",
                                 (item_id,)).fetchone()
            if fresh is None:
                return 404, {"error": "no such item"}
            return 409, {
                "error": f"the item is no longer {prev}; it is now "
                         f"{fresh['state']}",
                "hint": "re-read the item and retry against its current "
                        "state"}
        left_course = None
        if unapproved_by_edit:
            # **ONE TRANSACTION WITH THE UPDATE ABOVE**: nothing has committed
            # yet. The reason is asked of the same comparison the note below
            # uses, so the Course history and the thread cannot disagree.
            left_course = _leave_course(
                conn, item, ident,
                "retyped while approved" if effective_type != item["type"]
                else "specification or definition of done edited without "
                     "end_state_unchanged")
        lines = [f"Field correction by {ident.actor}."]
        for field, old_val, new_val in changes:
            # The two prose fields are digested. A specification runs to
            # MAX_ITEM_TEXT, and a thread embedding two full copies of one on
            # every edit becomes the most expensive thing to read on the item.
            if field in ("specification", "definition_of_done"):
                old_val, new_val = _digest_prose(old_val), _digest_prose(new_val)
            lines.append(f"\n**{field}:** {old_val!r} → {new_val!r}")
        if unapproved_by_edit:
            # **THE NOTE SAYS WHAT TO DO DIFFERENTLY, NOT ONLY WHAT HAPPENED.**
            # It reported the revocation and stopped, so a caller learned the
            # outcome and not that it had a choice — and made the same call the
            # next time. Six approvals were destroyed in one evening that way on
            # 2026-08-27, by rewrites that stripped narrative while leaving the
            # end state exactly as approved.
            lines.append(
                "\n\n**Returned to `proposed`.** An approval attaches to what "
                "was approved — its type, and an epic's specification — so "
                "changing either means the operator has not yet agreed to what "
                "this item now is (ADR-0030).")
            # **WHICH OF THE TWO CAUSES FIRED, ASKED RATHER THAN ASSUMED**
            # (reviewer and security-reviewer, PR #67, independently, and the
            # reviewer blocked on it). `_revokes_approval` demotes for either of
            # two reasons and this note fires on both, so the previous version's
            # closing clause — "a retype without it lands and demotes, as this
            # one did" — told a caller demoted by a plain specification edit that
            # it had retyped. False, and false in the branch the accompanying
            # test drove.
            #
            # The comment three lines above says the record is the only safety
            # this mechanism has and must not be able to lie. That was written
            # about the note it replaced and applies to this one exactly as much.
            # The remedy is the same as everywhere else tonight: ask the
            # condition rather than restate a story about it.
            if effective_type != item["type"]:
                lines.append(
                    "\n\nThe retype is what revoked it: an approval attaches to "
                    "what the item IS as well as to what it says. "
                    "`end_state_unchanged` does not cover this — sent alongside "
                    "a `type` change it is REFUSED with a 400 and nothing is "
                    "written, so there is no assertion that would have kept "
                    "this approval.")
            else:
                lines.append(
                    "\n\nIf this edit left the desired end state as it was — a "
                    "typo, a reflow, a sentence saying the same thing — send "
                    "`end_state_unchanged: true` with it and the approval "
                    "stands. That assertion is the editor's; nothing verifies "
                    "it, and it is recorded here beside the diff.")
            lines.append(_left_course_note(left_course))
        elif kept_by_assertion:
            # **THE CLAIM IS RECORDED BESIDE THE DIFF IT APPLIES TO, and that
            # placement is what the flag's safety rests on.** Nothing can check
            # the assertion — only the editor knows whether a rewrite moved the
            # end state — so what the mechanism can offer is attribution: the
            # actor who claimed it, on the thread, next to the text that changed.
            # A reader who disagrees demotes the item; a reader with no record
            # cannot even know a claim was made.
            lines.append(
                f"\n\n**Approval kept.** {ident.actor} asserted this edit does "
                f"not change the desired end state, so the approval it already "
                f"carried still stands (the operator, 2026-08-27). Nothing verifies that "
                f"claim; the diff above is what it was made about.")
        # **NO NOTE, NO COMMENT AND NO EVENT WHEN `parent_id` IS THE ONLY
        # THING THAT CHANGED** (the operator's ruling, Slate a-v5j93k,
        # 2026-10-09). `changes` excludes `parent_id` by construction (see
        # where `columns["parent_id"]` is set above), so it is empty exactly
        # when a move was the whole request — a retype that also moved the
        # item still has its own entry in `changes` and still gets a note.
        if changes:
            now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
            conn.execute(
                "INSERT INTO issue_comments (item_id, actor, kind, body, run_id, at) "
                "VALUES (?,?,?,?,?,?)",
                (item_id, ident.actor, "note",
                 store.clip("\n".join(lines), store.MAX_ITEM_TEXT), None,
                 now.isoformat(" ")))
        conn.commit()
        fresh = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                             (item_id,)).fetchone()
        return 200, store.item_dict(
            fresh, claim=lease.claim_status(conn, fresh),
            prerequisites=store.prerequisite_status(
                conn, fresh, in_scope=_scope_predicate(ident)))
    finally:
        conn.close()


@app.post(APPROVE_ROUTE)
async def approve_epic(request: Request, item_id: str):
    """Approve an epic and put it on its product's live Course, as one act.

    **THE ONLY ROUTE THAT SETS `maturity` TO `approved`**
    (issue_01M32CA70BNV0WEW150V3KYBJ9). `/correct` and `/revision` refuse the
    field by name and point here, and `POST /items` has never accepted it, so no
    agent credential reaches `approved` at any protocol version.

    **THE CONTROL IS THE CREDENTIAL BOUNDARY PLUS A REQUIRED HUMAN, AND NEITHER
    IS A HUMAN `kind`.** The caller must hold `epic.approve`, which only an
    `approved_client` resolves to (`identity.HUMAN_RESERVED_CAPS`) and which an
    operator narrows off a client that must not approve. It must also name the
    person the client acted for in `acting_human`, through `attested_human`,
    which records that person and checks nothing about them: the capability
    authorizes and the human is the record. Gating on `ident.kind == "human"`
    would build a dead route, because `resolve()` never produces one. It also
    needs `course.write`, because approving writes a Course.

    **APPROVAL AND PLACEMENT COMMIT TOGETHER OR NOT AT ALL.** "No epic is
    approved and absent from a Course" cannot hold if the two writes are two
    transactions. The epic is appended at the END of the order, an epic already
    on the Course keeps its position and is not duplicated, and a product with
    no live Course refuses the approval and says that is why.

    **A LEASE DOES NOT REFUSE AN APPROVAL.** Approving changes only `maturity`
    and the Course, so another holder's live lease on the epic or its subtree
    neither refuses it nor is changed by it. A holder's later revision that
    changes the specification or definition of done still revokes the approval
    (`_revokes_approval`).

    **THE RECORD NAMES WHICH TEXT IT COVERS** (ADR-0030, CQ·146). The `approval`
    object carries `specification_sha256` and `definition_of_done_sha256`, the
    full sha256 hex digest of each field as stored in this same transaction —
    `null` when a field is missing or empty, never the hash of an empty string,
    so "was anything there" stays a distinct question from "does it match".
    Comparing a digest recorded here against a fresh hash of the current text is
    what turns "did the operator approve this" from archaeology over prose into
    arithmetic. Protocol version 1 only adds response fields within a version
    (`platform/contracts/library-protocol` §1); a client built before this
    field existed sees the fields it always saw, plus these two, and ignores
    them.

    Body: `acting_human` (required) and optionally `owner`, which is accepted
    so existing clients do not break and is not used. Returns the item and
    an `approval` record naming the approving client (`approved_by`), the human
    (`acting_human`), the Course and the 1-based `position` the epic took. The
    same facts, plus the two digests, are written to the item's thread and to
    the Course's history where that approval already writes one.
    """
    ident, err = _identify(request)
    if err:
        return err
    for capability in ("epic.approve", "course.write"):
        if not identity.authorized(ident, capability):
            return refusals.forbidden(capability)
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    # NEVER FALLS BACK TO `ident.actor`, as the signal-ack path does not: writing
    # the client into this field would record it as the person who approved.
    human, attest_err = identity.attested_human(ident, payload)
    if attest_err:
        return refusals.refuse(403, attest_err["error"])
    owner, err = _claim_owner(payload, ident)
    if err:
        return err
    try:
        status, body = await run_in_threadpool(
            _do_approve, ident, item_id, human, owner)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    return JSONResponse(status_code=status, content=body)


def _do_approve(ident, item_id: str, human: str, owner: str):
    conn = store.connect_rw()
    try:
        # **THE WRITE LOCK BEFORE THE FIRST READ, AND IT IS NOT THE LEASE.** The
        # Course list below is read, extended and written back with no version
        # predicate, so two overlapping approvals would each append to the same
        # stale list and one epic would end up approved and off the Course.
        # `_hold_for_write` used to take this lock as a side effect; with the
        # hold gone it is taken here, and it serializes placement and nothing
        # about who holds the epic (library#146 round 1).
        conn.execute("BEGIN IMMEDIATE")
        item = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                            (item_id,)).fetchone()
        if item is None or not identity.in_scope(ident, item["product"]):
            nf = refusals.not_found()
            return nf.status_code, json.loads(nf.body)
        if item["type"] != store.ITEM_TYPE_EPIC:
            return 400, {
                "error": f"only an epic is approved here, and {item_id} is a "
                         f"work item",
                "hint": "a Course contains only epics; work outside the plan "
                        "is not approved"}
        if item["state"] in store.ITEM_TERMINAL:
            return 409, {"error": f"item is {item['state']}; a terminal epic is "
                                  f"not approved"}
        # No `_hold_for_write`: an approval changes only `maturity` and the
        # Course, edits no text a holder is working on, and a holder's later
        # edit to the end state already revokes it (`_revokes_approval`).
        # Another holder's lease therefore protects nothing here.
        # The specification-and-definition-of-done requirement, through the one
        # function the demotion rules use so the two cannot drift.
        err = _approval_invariant(
            item, effective_type=item["type"], has_maturity=True,
            new_maturity=store.ITEM_MATURITY_APPROVED, has_spec=False,
            new_spec=None, has_dod=False, new_dod=None)
        if err:
            return err
        # The full digest of each field AS STORED IN THIS TRANSACTION, so the
        # record names exactly the text this approval covers (ADR-0030,
        # CQ·146). Computed once and reused in the response, the thread note
        # and the Course event below, so the three cannot drift from each
        # other or from a second read of the row.
        spec_sha256 = _full_sha256(item["specification"])
        dod_sha256 = _full_sha256(item["definition_of_done"])
        course = _live_course_of(conn, item["product"])
        if course is None:
            return 409, {
                "error": f"{item['product']} has no live Course, so {item_id} "
                         f"cannot be approved: approving an epic puts it on "
                         f"the Course",
                "reason": "no_live_course",
                "hint": "create the product's Course (POST /courses), then "
                        "approve"}
        entries = store._json_or(course["entries"], [])
        placed = item_id in entries
        if not placed and len(entries) >= store.MAX_COURSE_ENTRIES:
            return 409, {
                "error": f"the Course already holds {store.MAX_COURSE_ENTRIES} "
                         f"epics, the most it may",
                "course_id": course["id"]}
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(" ")
        newly_approved = item["maturity"] != store.ITEM_MATURITY_APPROVED
        if newly_approved:
            # Conditional on the state this request read, as `/correct` is.
            cur = conn.execute(
                "UPDATE issue_items SET maturity = ? WHERE id = ? AND state = ?",
                (store.ITEM_MATURITY_APPROVED, item_id, item["state"]))
            if cur.rowcount != 1:
                conn.rollback()
                return 409, {
                    "error": f"the item is no longer {item['state']}",
                    "hint": "re-read the item and retry against its current "
                            "state"}
        restored = False
        if not placed:
            index = _index_before_demotion(conn, course["id"], item_id, entries)
            if index is None:
                entries.append(item_id)
            else:
                entries.insert(index, item_id)
                restored = True
            conn.execute(
                "UPDATE courses SET entries = ?, version = version + 1, "
                "updated_by = ?, updated_at = ? WHERE id = ?",
                (json.dumps(entries), ident.actor, now, course["id"]))
            # `entries` is a typed snapshot of Course membership (a list of
            # epic ids) read elsewhere as exactly that; it is not a general
            # payload column, so the digests go in `note`, the free-text
            # column every other course_events row already narrates into.
            conn.execute(
                "INSERT INTO course_events (course_id, actor, action, entries, "
                "note, at) VALUES (?,?,?,?,?,?)",
                (course["id"], ident.actor, store.COURSE_ACTION_APPROVED,
                 json.dumps(entries),
                 f"{item_id} approved by {ident.actor} for {human} "
                 f"(specification_sha256={spec_sha256 or 'none'}, "
                 f"definition_of_done_sha256={dod_sha256 or 'none'})"
                 + (f"; returned to position {entries.index(item_id) + 1}, "
                    f"where it stood before its demotion" if restored else ""),
                 now))
        position = entries.index(item_id) + 1
        if newly_approved or not placed:
            conn.execute(
                "INSERT INTO issue_comments (item_id, actor, kind, body, run_id, "
                "at) VALUES (?,?,?,?,?,?)",
                (item_id, ident.actor, "note", store.clip(
                    f"**Approved** by {ident.actor} for {human}. "
                    f"{'Placed on' if not placed else 'Already on'} Course "
                    f"{course['id']} at position {position} of {len(entries)}"
                    f"{', where it stood before its demotion' if restored else ''}"
                    f".\n\n"
                    f"specification_sha256: {spec_sha256 or 'none'}\n"
                    f"definition_of_done_sha256: {dod_sha256 or 'none'}",
                    store.MAX_ITEM_TEXT), None, now))
        conn.commit()
        fresh = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                             (item_id,)).fetchone()
        return 200, {
            "item": store.item_dict(
                fresh, claim=lease.claim_status(conn, fresh),
                prerequisites=store.prerequisite_status(
                    conn, fresh, in_scope=_scope_predicate(ident))),
            "approval": {"approved_by": ident.actor, "acting_human": human,
                         "course_id": course["id"], "position": position,
                         "course_size": len(entries),
                         "newly_placed": not placed,
                         "restored_position": restored,
                         "specification_sha256": spec_sha256,
                         "definition_of_done_sha256": dod_sha256}}
    finally:
        conn.close()


@app.post(MODEL_ROUTE)
async def set_item_model(request: Request, item_id: str):
    """Set or clear the operator's model choice on a work item.

    **THE ONLY ROUTE THAT WRITES `model_choice`**
    (issue_01M38WTBNAS3NBGEYR8349RR52). `/correct`, `/revision` and
    `POST /items` refuse the field by name and point here. The Bosun's
    autonomous path runs an item on a model other than its roster pin only when
    this field says an operator chose it, so a field an agent could write would
    remove that pin across the whole overnight fleet.

    **WHAT THE ATTESTATION IS, AND IS NOT.** The caller must hold `item.model`,
    which only an `approved_client` resolves to (`identity.HUMAN_RESERVED_CAPS`)
    and which an operator narrows off a client that must not set it. That
    capability is the authorization: the Bosun can trust that an approved
    client, and no agent, wrote the choice. The route also requires an
    `acting_human`, through `attested_human`, and that is a RECORD: the Library
    stores the string the client reported and does not verify the person,
    because it has no verified person identity until client authentication
    lands (issue_01M0VPY5GVK3WAFFV3GJCJTAM4). The client name is taken from the
    authenticated credential, never from the body.

    **A CLEAR IS THE SAME ROUTE.** `model: null` removes the choice and is
    recorded the same way. Every set and clear writes a comment to the item's
    thread naming the client, the human, the model and the reason; that thread
    is the history, since only the current choice is stored.

    **A CHECKED-OUT WRITE LIKE ANY OTHER** (`_hold_for_write`): pass `owner` if
    you hold the item; another holder's live lease refuses it.

    Body: `model` (a non-empty string to set, or null to clear; required),
    `acting_human` (required), optionally `reason` and `owner`. Returns the item,
    whose `model_choice` is the stored choice or null.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "item.model"):
        return refusals.forbidden("item.model")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    # NEVER FALLS BACK TO `ident.actor`, as `approve_epic` does not.
    human, attest_err = identity.attested_human(ident, payload)
    if attest_err:
        return refusals.refuse(403, attest_err["error"])
    if "model" not in payload:
        return refusals.refuse(
            400, "model is required: a model name to set, or null to clear")
    model = payload["model"]
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            return refusals.refuse(
                400, "model must be a non-empty string, or null to clear")
        model = model.strip()
        if len(model) > store.MAX_IDENT:
            return refusals.refuse(
                400, f"model is longer than {store.MAX_IDENT} characters")
    reason = payload.get("reason")
    if reason is not None:
        if not isinstance(reason, str):
            return refusals.refuse(400, "reason must be a string when present")
        reason = reason.strip() or None
        if reason is not None and len(reason) > store.MAX_TITLE:
            return refusals.refuse(
                400, f"reason is longer than {store.MAX_TITLE} characters")
    owner, err = _claim_owner(payload, ident)
    if err:
        return err
    try:
        status, body = await run_in_threadpool(
            _do_set_model, ident, item_id, human, owner, model, reason)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    return JSONResponse(status_code=status, content=body)


def _do_set_model(ident, item_id: str, human: str, owner: str,
                  model: str | None, reason: str | None):
    conn = store.connect_rw()
    try:
        item = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                            (item_id,)).fetchone()
        if item is None or not identity.in_scope(ident, item["product"]):
            nf = refusals.not_found()
            return nf.status_code, json.loads(nf.body)
        if item["state"] in store.ITEM_TERMINAL:
            return 409, {"error": f"item is {item['state']}; a terminal item's "
                                  f"model is not changed"}
        refusal = _hold_for_write(conn, ident, item, owner,
                                  act="a model choice")
        if refusal:
            return refusal
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(" ")
        if model is None:
            columns = (None, None, None, None, None)
            text = (f"**Model choice cleared** by {ident.actor} for {human}"
                    + (f": {reason}" if reason else "."))
        else:
            columns = (model, reason, ident.actor, human, now)
            text = (f"**Model set to `{model}`** by {ident.actor} for {human}"
                    + (f": {reason}" if reason else "."))
        # Conditional on the state this request read, as `/correct` is.
        cur = conn.execute(
            "UPDATE issue_items SET model_choice = ?, model_reason = ?, "
            "model_set_by_client = ?, model_acting_human = ?, model_set_at = ? "
            "WHERE id = ? AND state = ?",
            (*columns, item_id, item["state"]))
        if cur.rowcount != 1:
            conn.rollback()
            return 409, {
                "error": f"the item is no longer {item['state']}",
                "hint": "re-read the item and retry against its current state"}
        conn.execute(
            "INSERT INTO issue_comments (item_id, actor, kind, body, run_id, "
            "at) VALUES (?,?,?,?,?,?)",
            (item_id, ident.actor, "note",
             store.clip(text, store.MAX_ITEM_TEXT), None, now))
        conn.commit()
        fresh = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                             (item_id,)).fetchone()
        return 200, {
            "item": store.item_dict(
                fresh, claim=lease.claim_status(conn, fresh),
                prerequisites=store.prerequisite_status(
                    conn, fresh, in_scope=_scope_predicate(ident)))}
    finally:
        conn.close()


@app.post("/items/{item_id}/prerequisites/verdict")
async def judge_prerequisite(request: Request, item_id: str):
    """Set whether a text prerequisite is met.

    **Gated on `item.resolve`, the same capability `/correct` uses**, not a
    dedicated one (ADR platform/decisions/0074). A
    credential-restricted gate here is a guard with no seen occurrence of a
    crew clearing a condition it should not have (ADR-0068), and it would
    also make a text prerequisite slower to clear than the parking it
    replaces — a crew that has just delivered the condition would otherwise
    wait for a weekly sweep to hold the one credential allowed to judge it.

    A dict/item-shaped prerequisite is never a target here: only a text entry
    carries a `met` flag to set, so this route matches on `text` rather than
    on a position in the list, which stays stable across an unrelated
    `/correct` replacing a different entry.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "item.resolve"):
        return refusals.forbidden("item.resolve")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return refusals.refuse(400, "text is required")
    text = text.strip()
    met = payload.get("met")
    if not isinstance(met, bool):
        return refusals.refuse(400, "met must be true or false")
    owner, err = _claim_owner(payload, ident)
    if err:
        return err
    try:
        result = await run_in_threadpool(
            _do_judge_prerequisite, ident, item_id, text, met, owner)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    status, body = result
    return JSONResponse(status_code=status, content=body)


def _do_judge_prerequisite(ident, item_id: str, text: str, met: bool,
                           owner: str):
    conn = store.connect_rw()
    try:
        # **`BEGIN IMMEDIATE` BEFORE THE READ**, matching `_do_release`'s
        # reasoning: this write names no prior column value to compare
        # against — there is no `state` this verdict is conditional on — so
        # the guard against a concurrent `/correct` replacing the whole
        # `prerequisites` list between this read and this write has to be the
        # lock rather than a CAS predicate.
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        item = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                            (item_id,)).fetchone()
        if item is None or not identity.in_scope(ident, item["product"]):
            # `refusals.not_found()`'s wording, decoded back into this
            # function's `(status, body)` convention rather than repeated —
            # every other item route answers a missing-or-out-of-scope item
            # this same way (platform/contracts/library-protocol §8).
            nf = refusals.not_found()
            return nf.status_code, json.loads(nf.body)
        refusal = _hold_for_write(conn, ident, item, owner, act="a verdict")
        if refusal:
            return refusal
        entries = store._prerequisite_entries(item["prerequisites"])
        found = None
        for e in entries:
            if e.get("text") == text:
                found = e
                break
        if found is None:
            return 404, {"error": "no prerequisite with that text"}
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        found["met"] = met
        found["judged_by"] = ident.actor
        found["judged_at"] = now.isoformat(" ")
        conn.execute("UPDATE issue_items SET prerequisites = ? WHERE id = ?",
                     (json.dumps(entries), item_id))
        conn.execute(
            "INSERT INTO issue_comments (item_id, actor, kind, body, run_id, at) "
            "VALUES (?,?,?,?,?,?)",
            (item_id, ident.actor, "note",
             store.clip(
                 f"Prerequisite judged {'met' if met else 'unmet'} by "
                 f"{ident.actor}: {text!r}", store.MAX_ITEM_TEXT),
             None, now.isoformat(" ")))
        conn.commit()
        fresh = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                             (item_id,)).fetchone()
        return 200, store.item_dict(
            fresh, claim=lease.claim_status(conn, fresh),
            prerequisites=store.prerequisite_status(
                conn, fresh, in_scope=_scope_predicate(ident)))
    finally:
        conn.close()


# **REFUSED BY NAME RATHER THAN IGNORED.** A revision that silently dropped a
# field a caller sent would report success over a change that did not happen, and
# each of these has a route that DOES record it — so the refusal names that route
# instead of leaving the caller to guess which door it wanted.
_REVISION_FORBIDDEN = {
    "product": "a product change moves the whole subtree, so it has its own "
               "route: POST /items/{item_id}/product. The descendant-count "
               "disclosure on epic retype depends on a subtree never spanning "
               "products (ADR-0023)",
    "parent_id": "re-parenting is a correction to where an item sits rather than "
                 "to its content. Use /correct, passing the same owner: your "
                 "own lease never refuses it",
    "type": "retyping is a correction to a misfiling rather than a revision of "
            "content, and retyping an epic with children corrupts the tree. "
            "Use /correct",
    "state": "state moves through /checkout, /release and /resolve, which record "
             "the transition",
    "needs": "who can execute an item is a triage fact rather than its content. "
             "Use /correct",
    "severity": "severity is a triage fact rather than content. Use /correct",
    "refs": "refs are set at /items on create or read back as-is; a revision "
            "changes an item's content, not its references",
}


def _briefing(conn, item, ident, *, vectors_unavailable: str | None = None) -> dict:
    """The adjacency briefing for one item, scoped to this caller.

    **A BRIEFING THAT THROWS MUST NOT COST THE CALLER ITS LOCK OR ITS REVISION.**
    Both callers reach here after their write has committed, so the only thing a
    failure can still take is the briefing itself. It becomes `available: false`
    with the cause named — which a reader can act on, where a 500 over a completed
    checkout is a caller left unsure whether it holds anything.

    **`vectors_unavailable`, when given, is `vectors.connect_rw_if_available()`'s
    own reason, and this returns `unavailable` on it without ever querying `conn`**
    (issue_01M35R5GA1SKRG2CMQRF189NDE, round 1). `conn` in that case has no vec0
    loaded and no vectors database attached — a query would fail anyway — but
    running it would report a generic "OperationalError" through the `except`
    below instead of the specific, already-known reason a caller could act on.
    """
    if vectors_unavailable is not None:
        return adjacency.unavailable(vectors_unavailable)
    try:
        return adjacency.neighbours_of(
            conn, item, in_scope=lambda product: identity.in_scope(ident, product))
    except Exception as exc:  # noqa: BLE001 — any failure means "not computed"
        print(f"adjacency failed for {item['id']}: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return adjacency.unavailable(
            f"the briefing could not be computed ({type(exc).__name__}); this is "
            f"UNKNOWN rather than 'no near duplicates'")


@app.post("/items/{item_id}/product")
async def move_item_product(request: Request, item_id: str):
    """Move a work item, and everything beneath it, to another product
    (issue_01M0AT44FBFJ7CHP84V027PG7J).

    Body: `product` (a slug, alias, repo name or non-project scope, resolved
    through the registry as `POST /items` resolves it) and `owner`, the same
    stable identity `/correct` takes. Needs `item.resolve`, and the caller's
    scope must cover BOTH products: moving an item into a product the caller
    cannot see would disclose it there, and moving one out of a product the
    caller cannot see would take it.

    **THE WHOLE SUBTREE MOVES, IN ONE TRANSACTION, OR NOTHING DOES.** Every
    parent edge is same-product, which is what keeps the descendant count
    `/correct` discloses on an epic retype safe: a subtree never spans
    products. Moving the item alone would break that, so its descendants move
    with it, and a subtree that cannot be enumerated is refused rather than
    half-moved.

    **THE MOVED ITEM LEAVES ITS PARENT.** An epic becomes top level in the new
    product; a work item lands in the new product's corrections queue. Its
    descendants keep their parent edges, which now all sit in the new product.

    **EACH MOVED ITEM GETS A NEW KEY IN THE NEW PRODUCT** (`_rekey_for_move`),
    because keys are numbered per product and the old one may already be taken
    there. The old key is never handed out again in the old product, so a
    reference to it finds nothing rather than the wrong item, and each item's
    history records old key and new.

    **REFUSED, BY NAME, WHEN `found_during` WOULD CROSS PRODUCTS.**
    `found_during` must name an epic in the item's own product, so a move that
    carries an item away from the epic it names, or carries away an epic that
    an item left behind names, is refused with those items listed. Correct
    `found_during` with `/correct` first; clearing it here would lose a fact
    nobody asked to lose.

    An approved epic in the moved subtree leaves the old product's live
    Course, recorded in the Course's history, as a demotion records it. It
    joins no Course in the new product: what that product works on next is
    its own plan's decision.

    Returns `{item, moved}`, where `moved` lists `{id, old_key, new_key}` for
    every item that moved.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "item.resolve"):
        return refusals.forbidden("item.resolve")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    owner, err = _claim_owner(payload, ident)
    if err:
        return err
    raw = payload.get("product")
    if not isinstance(raw, str) or not raw.strip():
        return refusals.refuse(400, "product is required")
    if len(raw) > store.MAX_IDENT:
        return refusals.refuse(
            400, f"product is longer than {store.MAX_IDENT} characters")
    # The same resolution, and the same narrowed refusal, `POST /items` gives:
    # see the note there on why the known list is only what this caller may use.
    product = projects.resolve(raw)
    if product is None:
        known = [n for n in projects.known_names()
                 if identity.in_scope(ident, projects.resolve(n))]
        return refusals.refuse(
            400, f"no project or scope named {raw!r}",
            hint="a slug, alias, repo name or non-project scope resolves; the "
                 "canonical slug is what gets stored",
            known=known)
    if not identity.in_scope(ident, product):
        return refusals.not_found()
    try:
        status, body = await run_in_threadpool(
            _do_move_product, ident, item_id, product, owner)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    return JSONResponse(status_code=status, content=body)


def _do_move_product(ident, item_id: str, product: str, owner: str):
    """Returns `(status, body)`."""
    conn = store.connect_rw()
    try:
        item = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                            (item_id,)).fetchone()
        if item is None or not identity.in_scope(ident, item["product"]):
            return 404, {"error": "no such item"}
        if item["state"] in store.ITEM_TERMINAL:
            return 409, {"error": f"item is {item['state']}; a terminal item is "
                                  f"a record of what happened and does not move"}
        if item["product"] == product:
            return 409, {"error": f"{item_id} is already in {product!r}"}
        # Takes the write lock before it looks, so nothing can check out a part
        # of this subtree between the checks below and the move.
        refusal = _hold_for_write(conn, ident, item, owner, act="a move")
        if refusal:
            return refusal[0], refusal[1]
        try:
            # Root first, then breadth-first: `_rekey_for_move` needs every
            # parent keyed before its children.
            ids = [item_id, *lease.subtree_ids(conn, item_id)]
        except lease.SubtreeTooLarge as too_big:
            return 409, {
                "error": f"cannot enumerate this subtree: {too_big}",
                "hint": "a move takes the whole subtree or nothing, so it "
                        "cannot proceed until the subtree can be counted"}
        # `_hold_for_write` answers for the fence ABOVE the item. A move also
        # takes everything BELOW it, so a lease another holder has inside the
        # subtree refuses it too. **THE BLOCKING LEASE'S OWN PRODUCT IS CHECKED
        # BEFORE ITS HOLDER IS NAMED** (security-reviewer, knowledge#898), as at
        # every other disclosure site: that every descendant shares the item's
        # product is the invariant `_lease_root_visible` refuses to lean on.
        crew = _crew_of(ident)
        below = [c for c in lease.live_conflicts(
                     conn, item_id, ignore_holder=owner,
                     holder_crew=crew["crew"], crew_exempt=crew["crew_exempt"])
                 if c.blocks_by == "descendant"]
        if below:
            held = below[0].lease
            if not _lease_root_visible(conn, ident, held):
                return 409, {
                    "error": f"something beneath {item_id} is held by another "
                             f"session, and that lease is outside this "
                             f"identity's scope",
                    "reason": "held",
                    "hint": "the hold is real but its holder is not yours to "
                            "see; wait for it to release"}
            return 409, {
                "error": f"something beneath {item_id} is held by "
                         f"{held['holder']}; a move takes the whole subtree",
                "reason": "held",
                "lease": lease.lease_dict(held, conn=conn),
                "hint": "wait for the holder to release, or move a part of "
                        "the tree nobody holds"}
        moving = set(ids)
        marks = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT * FROM issue_items WHERE id IN ({marks})", ids).fetchall()
        by_id = {r["id"]: r for r in rows}
        leaving = sorted(r["id"] for r in rows
                         if r["found_during"] and r["found_during"] not in moving)
        staying = sorted(r["id"] for r in conn.execute(
            f"SELECT id FROM issue_items WHERE found_during IN ({marks}) "
            f"AND id NOT IN ({marks})", [*ids, *ids]))
        if leaving or staying:
            return 409, {
                "error": "found_during would cross products: it must name an "
                         "epic in the item's own product",
                "moving_away_from_their_epic": leaving,
                "left_behind_naming_a_moved_epic": staying,
                "hint": "correct found_during on these items with /correct "
                        "first, then move"}

        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(" ")
        old_product = item["product"]
        if item["type"] == store.ITEM_TYPE_EPIC:
            new_parent = None
        else:
            # THROUGH `_validate_parent`, with the DESTINATION product, like
            # every other parent write (`test_invariants`): it is what keeps a
            # parent edge from crossing products.
            new_parent, perr = _validate_parent(
                conn, store.get_or_create_corrections_queue(conn, product),
                ident=ident, product=product, item_id=item_id,
                item_type=item["type"])
            if perr:
                conn.rollback()
                return perr[0], perr[1]
        left_courses = []
        for row in rows:
            if row["type"] == store.ITEM_TYPE_EPIC:
                left = _leave_course(conn, row, ident,
                                     f"moved from {old_product} to {product}")
                if left is not None:
                    left_courses.append({"item_id": row["id"], **left})
        new_keys = _rekey_for_move(conn, ids, by_id, product, item_id,
                                   new_parent)
        moved = []
        for mid in ids:
            row = by_id[mid]
            # Only the moved root changes parent; its descendants keep theirs,
            # which all move with them.
            columns = {"product": product, "item_key": new_keys[mid]}
            parent = row["parent_id"]
            if mid == item_id:
                parent = columns["parent_id"] = new_parent
            sets = ", ".join(f"{c} = ?" for c in columns)
            conn.execute(f"UPDATE issue_items SET {sets} WHERE id = ?",
                         [*columns.values(), mid])
            note = (f"Moved by {ident.actor} from {old_product!r} to "
                    f"{product!r}; key {row['item_key']!r} → "
                    f"{new_keys[mid]!r}")
            if mid == item_id and parent != row["parent_id"]:
                note += f"; parent {row['parent_id']!r} → {parent!r}"
            conn.execute(
                "INSERT INTO issue_events (item_id, actor, from_state, "
                "to_state, at, note) VALUES (?,?,?,?,?,?)",
                (mid, ident.actor, row["state"], row["state"], now, note))
            moved.append({"id": mid, "old_key": row["item_key"],
                          "new_key": new_keys[mid]})
        conn.commit()
        fresh = conn.execute(f"SELECT {store.ITEM_COLUMNS} FROM issue_items "
                             f"WHERE id = ?", (item_id,)).fetchone()
        return 200, {
            "item": store.item_dict(
                fresh, claim=lease.claim_status(conn, fresh),
                prerequisites=store.prerequisite_status(
                    conn, fresh, in_scope=_scope_predicate(ident))),
            "moved": moved,
            "left_courses": left_courses}
    finally:
        conn.close()


def _rekey_for_move(conn, ids, by_id, product: str, root_id: str,
                    root_parent) -> dict:
    """The key each item in a moving subtree will carry in `product`.

    Minted as `mint_item_key` would mint them were they filed there today: an
    epic gets the next `E<n>` of the new product, the moved root, when it is a
    work item, gets the next `CQ·n` there, and a work item under a moved epic
    extends that epic's NEW key. Where a sub-item's old key extends its
    parent's old key (`E9·4` under `E9`), the suffix is kept (`E3·4` under the
    new `E3`), so the numbers a person knew survive the move; a sub-item filed
    under another epic and re-parented since carries a suffix from that epic's
    counter, so it gets the next number of its parent's counter instead, which
    is never handed out twice.

    `ids` is in `subtree_ids` order, root first and each parent before its
    children, so a parent's new key exists before a child extends it.
    """
    out = {}
    for mid in ids:
        row = by_id[mid]
        parent_id = root_parent if mid == root_id else row["parent_id"]
        if row["type"] == store.ITEM_TYPE_EPIC:
            out[mid] = store.mint_item_key(conn, product=product,
                                           item_type=row["type"],
                                           parent_id=parent_id)
            continue
        parent = by_id.get(parent_id)
        if parent is None:
            out[mid] = store.mint_item_key(conn, product=product,
                                           item_type=row["type"],
                                           parent_id=None)
            continue
        prefix = f"{parent['item_key']}{store.ITEM_KEY_SEPARATOR}"
        old = row["item_key"] or ""
        if parent["item_key"] and old.startswith(prefix):
            out[mid] = f"{out[parent_id]}{store.ITEM_KEY_SEPARATOR}{old[len(prefix):]}"
        else:
            n = store._next_item_key_number(conn, store.ITEM_KEY_SCOPE_ITEM,
                                            parent_id)
            out[mid] = f"{out[parent_id]}{store.ITEM_KEY_SEPARATOR}{n}"
    return out


@app.post("/items/{item_id}/revision")
async def revise_item(request: Request, item_id: str):
    """Revise a held item in full — `title` and `body` included.

    **THE LEASE IS THE AUTHORIZATION**, which makes checkout the precondition of
    EDITING exactly as ARCH-424 makes it the precondition of working. The
    capability check still runs underneath it: holding a lease grants no access to
    a product the token could not already write.

    **AND THE LEASE IS A COLLISION GUARD RATHER THAN AN ISOLATION BOUNDARY.** The
    holder is a self-asserted label, so a caller already holding `item.resolve` on
    the product can name itself the holder and pass — here and at checkout alike.
    What isolates is the capability and the product scope, both checked first.
    Making a holder unforgeable needs a credential the caller cannot mint, which
    is Phase 5's token.

    **Say the asymmetry with re-parenting out loud, because a reader who learns
    one rule will guess the other wrong.** Re-parenting is REFUSED while a live
    lease covers an item; revising is PERMITTED only while one does. A lease
    protects the tree's SHAPE from outsiders and at the same time authorizes the
    holder's edits to the item's own CONTENT. The reason they point opposite ways
    is that moving an edge changes what somebody else's fence holds, while
    rewriting a body changes only the thing the holder already has.

    **The gap this closes is live.** When a decision makes an epic's body wrong,
    `/correct` does not accept `title` or `body`, so the holder posts a comment
    saying the specification supersedes a paragraph. An item that cannot be
    corrected accumulates annotations that contradict it.

    Body: `owner` (the same stable per-session identity you checked out as), plus
    at least one of `title`, `short_description`, `body`, `specification` or
    `definition_of_done`.
    Everything else is refused by name, `maturity` included: an epic is approved
    through `POST /items/{item_id}/approve`.

    Returns `{item, lease, adjacency}`.

    **A LIVE LEASE IS REQUIRED AT SUBMIT TIME, NOT MERELY AT CHECKOUT.** A
    revision arriving after the lease lapsed is refused, because the holder it was
    authorized for no longer holds it and someone else may already be inside the
    subtree. This is the rule a caller is most likely to get wrong: a long session
    that never heartbeats holds a dead lease and looks alive to itself.

    **REVISED TEXT DROPS ITS VECTORS IN THE SAME TRANSACTION AS THE WRITE, and is
    re-embedded after the commit.** Contract §5 requires exactly that ordering.
    Same transaction, so there is no window in which the row's stamp claims vectors
    describing text it no longer holds; after the commit, because the model is
    CPU-bound and would otherwise hold SQLite's single write lock for its whole
    run.

    **THE BRIEFING IS RECOMPUTED ON THE REVISED TEXT AND RECORDED ON THE ITEM**
    (§5), so a later reader sees what the holder was told and chose to proceed
    past, and Phase 9's duplicate-filing measurement has a source it otherwise
    lacks. It is computed after the re-embed rather than inside the write, since
    the revised text has no vectors until that lands — and it REPORTS rather than
    refuses: the KNN's distances are not calibrated to be a gate, so making them
    one would be a use they were never measured for.

    The approval invariant is `_approval_invariant`'s, shared with `/correct`
    rather than restated — a gate enforced at one door and not the other is worth
    less than a validation.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "item.resolve"):
        return refusals.forbidden("item.resolve")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    refused_maturity = (_refuse_maturity(payload)
                        or _refuse_model_choice(payload))
    if refused_maturity:
        return refused_maturity
    refused = sorted(k for k in _REVISION_FORBIDDEN if k in payload)
    if refused:
        return refusals.refuse(
            400, "this route revises an item's own content; "
                 + "; ".join(f"{k} — {_REVISION_FORBIDDEN[k]}" for k in refused),
            refused=refused)
    owner, err = _claim_owner(payload, ident)
    if err:
        return err
    has = {name: name in payload for name in
           ("title", "short_description", "body", "specification",
            "definition_of_done")}
    if not any(has.values()):
        return refusals.refuse(
            400, "at least one of title, short_description, body, "
                 "specification or definition_of_done is required")
    # **`title` IS THE ONE FIELD WITH NO NULL.** An item with no title is
    # unreadable in every list that shows one. `body` may be cleared, the same as
    # the two prose fields: a description that turned out to be wrong is better
    # empty than wrong.
    new_title = payload.get("title")
    if has["title"]:
        if not isinstance(new_title, str) or not new_title.strip():
            return refusals.refuse(400, "title must be a non-empty string")
        new_title = new_title.strip()
        if len(new_title) > store.MAX_TITLE:
            return refusals.refuse(
                400, f"title is longer than {store.MAX_TITLE} characters",
                length=len(new_title))
    new_body = payload.get("body")
    if has["body"] and new_body is not None:
        if not isinstance(new_body, str):
            return refusals.refuse(
                400, "body must be a string, or null to clear it")
        if len(new_body) > store.MAX_ITEM_TEXT:
            return refusals.refuse(
                400, f"body must be at most {store.MAX_ITEM_TEXT} characters",
                length=len(new_body))
    # Through the SAME validators create and correct use, so the three doors
    # cannot drift into accepting different things.
    new_spec, spec_err = _validate_prose(payload, "specification")
    new_dod, dod_err = _validate_prose(payload, "definition_of_done")
    new_short, short_err = _validate_short_description(payload)
    err = spec_err or dod_err or short_err
    if err:
        return JSONResponse(status_code=err[0], content=err[1])
    # **NO RETYPE CONFLICT TO CHECK ON THIS ROUTE**: `type` is in
    # `_REVISION_FORBIDDEN` and refused by name above, so a revision cannot
    # change what the item is. `_do_correct` calls `_retype_conflict` after
    # loading the item; there is nothing here for it to compare.
    unchanged, err = _end_state_unchanged(payload)
    if err:
        return err

    try:
        result = await run_in_threadpool(
            functools.partial(
                _do_revision, ident, item_id, owner, has,
                {"title": new_title, "short_description": new_short,
                 "body": new_body, "specification": new_spec,
                 "definition_of_done": new_dod},
                end_state_unchanged=unchanged))
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    status, body, note_id = result
    if status == 200:
        # **UNCONDITIONALLY, AND THE IDEMPOTENCE IS WHAT MAKES THAT RIGHT.**
        # `embed_item_by_id` returns the existing stamp and writes nothing when
        # `embedded_at` is set, so a revision that changed no text costs one read.
        # The alternative — signalling "text moved" out of the write path so the
        # route can decide — puts the correctness of the index in a boolean two
        # functions have to agree about, where the stamp already knows. It also
        # repairs an item that was never embedded, which a conditional call would
        # skip precisely because nothing changed.
        await _embed_after_commit(vectors.VAULT_CHART_HOUSE, item_id)
        if note_id is not None:
            # **THE BRIEFING IS COMPUTED AFTER THE RE-EMBED, WHICH IS WHY IT
            # CANNOT LIVE IN `_do_revision`.** §5 wants it recomputed on the
            # REVISED text, and the revised text has no vectors until the embed
            # above lands — which itself has to be after the commit, because the
            # model would otherwise hold SQLite's write lock for its whole run.
            body["adjacency"] = await run_in_threadpool(
                _revision_briefing, ident, item_id, note_id)
    return JSONResponse(status_code=status, content=body)


def _revision_briefing(ident, item_id: str, note_id: int) -> dict:
    """The briefing on the revised text, appended to the revision's own note.

    **ONE COMMENT PER REVISION, so this APPENDS to the note the revision already
    wrote rather than adding a second row.** A row per briefing doubles the rate
    an item's thread grows, and the cap is enforced only in `/comments` — so the
    first casualty of exhausting it is a HUMAN refused a comment because system
    notes spent the budget.

    Updating a comment is otherwise something this service does not do; the
    `/comments` route is append-only, deliberately, because a handoff contract
    that can change under the agent already acting on it is not one. This is the
    route completing its own note within the same request, not a caller rewriting
    history, and the note is written in two steps only because the briefing is not
    computable until after the commit.

    **What a crash between the two steps costs is the briefing section, not the
    revision record.** That is the right way round: the diff is the audit trail
    and the briefing is advice the holder can get again by reading the item.

    **A missing or unattachable vectors file degrades to the SAME `unavailable`
    briefing, not a 500** — this note is still appended, on a plain
    `store.connect_rw()`, with the section saying adjacency could not be
    computed and why (reviewer + security-reviewer, issue_01M35R5GA1SKRG2CMQRF189NDE
    round 1).
    """
    conn, vectors_reason = vectors.connect_rw_if_available()
    if conn is None:
        conn = store.connect_rw()
    try:
        item = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                            (item_id,)).fetchone()
        if item is None:
            return adjacency.unavailable(
                "the item was removed before the briefing could be computed")
        near = _briefing(conn, item, ident, vectors_unavailable=vectors_reason)
        # **APPENDED WITHOUT CLIPPING, BECAUSE A CLIPPED BRIEFING IS AN AMBIGUOUS
        # ONE** (reviewer, PR #19). Truncating the concatenation at
        # `MAX_ITEM_TEXT` would drop the section silently, leaving a thread that
        # reads exactly like "not computed" — the one distinction this module
        # goes out of its way to preserve everywhere else. So the section is
        # appended only if it fits, and a note with no room says which of the two
        # it means. Unreachable in practice: `_digest_prose` bounds every field
        # in the diff, so a note reaching 100,000 characters is a symptom rather
        # than a size.
        existing = conn.execute("SELECT body FROM issue_comments WHERE id = ?",
                                (note_id,)).fetchone()["body"] or ""
        section = _briefing_note(near)
        room = store.MAX_ITEM_TEXT - len(existing)
        if len(section) > room:
            section = (_BRIEFING_TOO_LONG if len(_BRIEFING_TOO_LONG) <= room
                       else "")
            print(f"briefing for {item_id} did not fit its note "
                  f"({len(existing)} of {store.MAX_ITEM_TEXT} characters used)",
                  file=sys.stderr)
        if section:
            conn.execute("UPDATE issue_comments SET body = ? WHERE id = ?",
                         (existing + section, note_id))
            conn.commit()
        return near
    finally:
        conn.close()


# What a note with no room left says. Short by design: it has to fit where the
# briefing did not, and it exists so a thread reader can tell "the briefing did
# not fit here" from "there was no briefing".
_BRIEFING_TOO_LONG = ("\n\n**Adjacency at check-in: in the response only.** This "
                      "note had no room left for it. UNKNOWN from this thread "
                      "rather than 'no near duplicates'.")


def _briefing_note(near: dict) -> str:
    """The briefing as a thread entry.

    **`available: false` is spelled out as UNKNOWN rather than left to the reader
    to infer**, because the whole point of the flag is that "not computed" and
    "nothing resembles this" are different facts and the second is the one nobody
    should ever conclude by accident.

    **THE UNTRUSTED NOTE TRAVELS WITH THE TITLES HERE TOO, and leaving it off was
    a real gap rather than an omission of ceremony** (security-reviewer, PR #19).
    `adjacency.UNTRUSTED_NOTE` ships as a sibling of `neighbours` on every JSON
    response precisely because a title is whatever somebody typed when filing, so
    anyone who can file in a product the caller can see chooses text that lands
    inside a reading agent's context. This thread entry is a SECOND surface
    carrying the same attacker-reachable field, and a more durable one — it is
    written to be read later as handoff context, which is the whole reason it
    exists. A guard added to one member of a family and not the other is partial
    coverage wearing the look of a control.
    """
    if not near.get("available"):
        return (f"\n\n**Adjacency at check-in: not computed.** "
                f"{near.get('reason')}.\n\nThis is UNKNOWN rather than 'no near "
                f"duplicates'.")
    neighbours = near.get("neighbours") or []
    if not neighbours:
        return ("\n\n**Adjacency at check-in: no near duplicates.** Nothing in "
                "the vault scored at or above the threshold in force.")
    lines = [f"\n\n**Adjacency at check-in: {len(neighbours)} near.** Recorded so "
             f"a later reader sees what the holder was told and chose to proceed "
             f"past."]
    for other in neighbours:
        lines.append(f"\n- `{other['id']}` ({other['relation']}, "
                     f"{other['similarity']}) — {other['title']}")
    lines.append(f"\n\n{adjacency.UNTRUSTED_NOTE}")
    return "".join(lines)


def _do_revision(ident, item_id: str, owner: str, has: dict, new: dict,
                 *, end_state_unchanged: bool = False):
    # THE VECTOR-AWARE HANDLE, WHEN ONE ATTACHES, because §5's invalidation has to
    # land in the same transaction as the text it invalidates — a plain
    # `store.connect_rw` reports `no such module: vec0` at the DELETE, mid-
    # transaction, on the write path. **A missing or unattachable vectors file
    # falls back to that plain connection instead of taking the revision down**
    # (reviewer + security-reviewer, issue_01M35R5GA1SKRG2CMQRF189NDE round 1):
    # there is nothing to invalidate in an index that isn't there, so the
    # revision itself proceeds and `vectors_reason` below just skips the DELETE.
    conn, vectors_reason = vectors.connect_rw_if_available()
    if conn is None:
        conn = store.connect_rw()
    try:
        item = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                            (item_id,)).fetchone()
        if item is None or not identity.in_scope(ident, item["product"]):
            return 404, {"error": "no such item"}, None
        if item["state"] in store.ITEM_TERMINAL:
            return 409, {"error": f"item is {item['state']}; a terminal item is "
                                  f"a record of what happened and is not revised "
                                  f"in place"}, None
        # THE LEASE CHECK, AND IT RUNS AT SUBMIT TIME. `_fence_holding` returns
        # only leases that are alive, so a lapsed one reads here exactly as no
        # lease does — the correct answer rather than a coincidence: the grant
        # expired, and the subtree is claimable by someone else.
        held = _fence_holding(conn, item_id)
        if held is None:
            return 409, {
                "error": f"no live lease covers {item_id}; a revision is "
                         f"authorized by the lease, so there is nothing here "
                         f"authorizing this one",
                "hint": "check the item out first. A lease that lapsed while you "
                        "worked is the same answer: the holder it was granted to "
                        "no longer holds it"}, None
        if held["holder"] != owner:
            # NAMES THE HOLDER, for the reason `_lease_refusal` does: a bare "no"
            # leaves an orchestrator with block or retry and both waste the run.
            # **AND CHECKS THE BLOCKING LEASE'S OWN PRODUCT FIRST**, because the
            # caller proved scope over the item it named rather than over the row
            # the lease sits on — see `_lease_root_visible`.
            if not _lease_root_visible(conn, ident, held):
                return 409, {
                    "error": f"{item_id} is held by another session, and that "
                             f"lease is outside this identity's scope",
                    "reason": "held",
                    "hint": "the hold is real but its holder is not yours to "
                            "see; wait for it to release"}, None
            return 409, {
                "error": f"{item_id} is held by {held['holder']}, not {owner}; "
                         f"the lease is what authorizes a revision",
                "reason": "held",
                "lease": lease.lease_dict(held, conn=conn),
                "hint": "revise as the holder you checked out as, or wait for "
                        "the holder to release"}, None
        # **THE CREW CHECK, AFTER THE HOLDER MATCH** (CQ·10; reviewer, PR 287
        # round 1). A caller that typed the real holder back as `owner` passes
        # the comparison above, so this route takes the same refusal
        # `_hold_for_write` gives the other lease-authorized writes.
        if _crew_mismatch(ident, _row_crew(held, "holder_crew")):
            return 409, _crew_mismatch_refusal(), None
        # The gates run BEFORE any mutation, the same as every other route here.
        # **`effective_type` IS THE ITEM'S OWN TYPE**, because this route cannot
        # retype: `type` is refused by name above.
        err = _approval_invariant(
            item, effective_type=item["type"],
            has_maturity=False, new_maturity=None,
            has_spec=has["specification"], new_spec=new["specification"],
            has_dod=has["definition_of_done"], new_dod=new["definition_of_done"],
            end_state_unchanged=end_state_unchanged)
        if err:
            return (*err, None)
        changes, columns = [], {}
        for name in ("title", "short_description", "body", "specification",
                     "definition_of_done"):
            if has[name] and new[name] != item[name]:
                changes.append((name, item[name], new[name]))
                columns[name] = new[name]
        spec_edited = any(f in columns
                          for f in ("specification", "definition_of_done"))
        # This route never sets `maturity` (`_refuse_maturity`, above), so the
        # value standing after this edit is always the item's own.
        final_maturity = item["maturity"]
        unapproved_by_edit = (
            final_maturity == store.ITEM_MATURITY_APPROVED
            and _revokes_approval(item, effective_type=item["type"],
                                  end_state_unchanged=end_state_unchanged,
                                  spec_changing=spec_edited))
        # Asked of the shared definition, and gated, for the reasons
        # `_do_correct` spells out — see the note there.
        kept_by_assertion = (
            end_state_unchanged
            and final_maturity == store.ITEM_MATURITY_APPROVED
            and _revokes_approval(item, effective_type=item["type"],
                                  spec_changing=spec_edited,
                                  end_state_unchanged=False))
        if unapproved_by_edit:
            changes.append(("maturity", item["maturity"],
                            store.ITEM_MATURITY_PROPOSED))
            columns["maturity"] = store.ITEM_MATURITY_PROPOSED
        if not changes:
            # AN IDEMPOTENT RESUBMISSION IS NOT A NO-OP FOR THE LEASE. Getting
            # here proves the caller still holds it, which is a sighting, and the
            # heartbeat is what keeps a quiet holder alive.
            touched = lease.touch(conn, item, owner) or held
            conn.commit()
            # **NO BRIEFING, AND NO NOTE TO APPEND ONE TO.** Nothing moved, so
            # recomputing would score the same text against the same corpus and
            # spend a model call to reproduce the briefing the caller already has.
            return 200, {
                "item": store.item_dict(
                    item, claim=lease.claim_status(conn, item),
                    prerequisites=store.prerequisite_status(
                        conn, item, in_scope=_scope_predicate(ident))),
                "lease": lease.lease_dict(touched, conn=conn),
                "adjacency": adjacency.unavailable(
                    "nothing changed, so no briefing was recomputed; the "
                    "previous one still stands")}, None
        # **CONDITIONAL ON THE STATE THIS REQUEST READ, the same compare-and-swap
        # checkout/resolve/link-pr all carry, and now `/correct` too**
        # (security-reviewer, PR #81, round 2). The SELECT above and every gate
        # since — the lease check included — take no lock on `issue_items`, so a
        # concurrent `/resolve` that commits in this window used to be silently
        # overwritten by this UPDATE — landing a revision on an item that had
        # already gone terminal, contradicting the refusal this same function
        # gives when it reads that state directly. The loser rolls back and is
        # told the state it collided with, the same as `_transition`.
        prev = item["state"]
        sets = ", ".join(f"{c} = ?" for c in columns)
        cur = conn.execute(f"UPDATE issue_items SET {sets} WHERE id = ? "
                           f"AND state = ?",
                           [*columns.values(), item_id, prev])
        if cur.rowcount != 1:
            conn.rollback()
            fresh = conn.execute("SELECT state FROM issue_items WHERE id = ?",
                                 (item_id,)).fetchone()
            if fresh is None:
                return 404, {"error": "no such item"}, None
            return 409, {
                "error": f"the item is no longer {prev}; it is now "
                         f"{fresh['state']}",
                "hint": "re-read the item and retry against its current "
                        "state"}, None
        left_course = None
        if unapproved_by_edit:
            # Same transaction as the update above; see `_leave_course`.
            left_course = _leave_course(
                conn, item, ident,
                "specification or definition of done edited without "
                "end_state_unchanged")
        lines = [f"Revision by {ident.actor} (holding the lease as {owner!r})."]
        for field, old_val, new_val in changes:
            # `body` joins the two prose fields in being digested rather than
            # quoted whole: it runs to MAX_ITEM_TEXT, and two versions sharing
            # their first 200 characters would otherwise read as identical while
            # the stored text moved.
            if field in ("specification", "definition_of_done", "body"):
                old_val, new_val = _digest_prose(old_val), _digest_prose(new_val)
            lines.append(f"\n**{field}:** {old_val!r} → {new_val!r}")
        if unapproved_by_edit:
            # Names the assertion for the reason `_do_correct` names it — see
            # the note there.
            #
            # **AND SAYS NOTHING ABOUT `type`, BECAUSE THIS ROUTE HAS NO `type`
            # FIELD** (security-reviewer, PR #67). The first version copied
            # `_do_correct`'s sentence verbatim, which told a `/revision` caller
            # to avoid an interaction this door cannot produce — `type` is in
            # `_REVISION_FORBIDDEN` and refused by name long before here. The two
            # notes differ because the two routes differ, which is the only
            # reason two texts are worth keeping over one.
            lines.append(
                "\n\n**Returned to `proposed`.** An approval attaches to what "
                "was approved, so changing it means the operator has not yet "
                "agreed to what this item now says (ADR-0030).")
            lines.append(
                "\n\nIf this edit left the desired end state as it was — a "
                "typo, a reflow, a sentence saying the same thing — send "
                "`end_state_unchanged: true` with it and the approval stands. "
                "That assertion is the editor's; nothing verifies it, and it is "
                "recorded here beside the diff.")
            lines.append(_left_course_note(left_course))
        elif kept_by_assertion:
            # Recorded for the reason `/correct` records it — see the note there.
            lines.append(
                f"\n\n**Approval kept.** {ident.actor} asserted this edit does "
                f"not change the desired end state, so the approval it already "
                f"carried still stands (the operator, 2026-08-27). Nothing verifies that "
                f"claim; the diff above is what it was made about.")
        # **THE INVALIDATION IS HERE, INSIDE THE TRANSACTION THAT WRITES THE NEW
        # TEXT** (contract §5). The delete and the revision land together, so
        # there is no window in which the row's stamp claims vectors describing
        # something else. Re-embedding happens after the commit, in the route.
        #
        # Only when the EMBEDDED text moved. `specification`, `definition_of_done`
        # and `maturity` are not part of what `vectors.text_for_item` embeds, so
        # dropping vectors for a spec edit would throw away a correct index and
        # buy nothing.
        if vectors_reason is None and any(f in ("title", "body") for f in columns):
            vectors.invalidate_item(conn, vectors.VAULT_CHART_HOUSE, item_id)
        # ONE COMMENT PER REVISION. The briefing is APPENDED to this row once the
        # re-embed lands, rather than written as a second one — a row per briefing
        # doubles the rate an item's thread grows, and the cap is enforced only in
        # `/comments`, so the first casualty of exhausting it is a HUMAN refused a
        # comment because system notes spent the budget.
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        note = conn.execute(
            "INSERT INTO issue_comments (item_id, actor, kind, body, run_id, at) "
            "VALUES (?,?,?,?,?,?)",
            (item_id, ident.actor, "note",
             store.clip("\n".join(lines), store.MAX_ITEM_TEXT), None,
             now.isoformat(" ")))
        # Every lease-bearing action a holder takes is a heartbeat, which keeps
        # the grace period honest without asking workers to run a keepalive loop.
        touched = lease.touch(conn, item, owner) or held
        note_id = note.lastrowid
        conn.commit()
        fresh = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                             (item_id,)).fetchone()
        # The briefing this body carries is a placeholder the route replaces once
        # the re-embed has landed. It is filled in rather than left absent so a
        # crash between the two steps answers with a shape a client can read.
        return 200, {"item": store.item_dict(
                         fresh, claim=lease.claim_status(conn, fresh),
                         prerequisites=store.prerequisite_status(
                             conn, fresh, in_scope=_scope_predicate(ident))),
                     "lease": lease.lease_dict(touched, conn=conn),
                     "adjacency": adjacency.unavailable(
                         "the briefing had not been computed when this response "
                         "was assembled; this is UNKNOWN rather than 'no near "
                         "duplicates'")}, note_id
    finally:
        conn.close()


@app.post("/items/{item_id}/comments")
async def comment_on_item(request: Request, item_id: str):
    """Append to an item's thread.

    Append-only: a handoff contract that can change under the agent already
    acting on it is not one.

    **A `diagnosis` on an unclaimed item is refused**, and the gate is partial by
    design rather than by omission. A diagnosis is the handoff contract a fixer
    consumes, so a forged one on unclaimed work is the shape worth stopping;
    requiring the item be claimed raises the bar without pretending to
    authenticate the claimant, since `claimed_by` is a self-declared label a
    caller can read off the item. The honest version of this check arrives with
    per-session tokens.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "item.comment"):
        return refusals.forbidden("item.comment")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    body = payload.get("body")
    if not isinstance(body, str) or not body.strip():
        return refusals.refuse(400, "body is required")
    kind = payload.get("kind") or "note"
    if kind not in store.ITEM_COMMENT_KINDS:
        return refusals.refuse(400, f"kind {kind!r} is not a known kind",
                               supported=sorted(store.ITEM_COMMENT_KINDS))
    run_id = payload.get("run_id")
    if run_id is not None and not isinstance(run_id, str):
        return refusals.refuse(400, "run_id must be a string when present")
    # CLIPPED, like every other route that takes one (reviewer, PR #9). Unclipped
    # it is accepted by SQLite today and raises on commit against a length-bound
    # column later — an unactionable 500 from a provenance field.
    run_id = store.clip(run_id, 100) if run_id else None
    try:
        result = await run_in_threadpool(_do_comment, ident, item_id, body, kind,
                                         run_id)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    status, out = result
    return JSONResponse(status_code=status, content=out)


def _do_comment(ident, item_id: str, body: str, kind: str, run_id):
    conn = store.connect_rw()
    try:
        item = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                            (item_id,)).fetchone()
        if item is None or not identity.in_scope(ident, item["product"]):
            return 404, {"error": "no such item"}
        if kind == "diagnosis" and item["claimed_by"] is None:
            return 409, {"error": "item is not claimed; claim it before posting "
                                  "a diagnosis"}
        n = conn.execute("SELECT COUNT(*) FROM issue_comments WHERE item_id = ?",
                         (item_id,)).fetchone()[0]
        if n >= store.MAX_COMMENTS_PER_ITEM:
            # PERMANENTLY full rather than rate-limited, so a caller retries
            # somewhere else rather than against this thread forever.
            return 409, {"error": f"item has {store.MAX_COMMENTS_PER_ITEM} "
                                  f"comments and its thread is full",
                         "hint": "open a new item rather than retrying here"}
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        cur = conn.execute(
            "INSERT INTO issue_comments (item_id, actor, kind, body, run_id, at) "
            "VALUES (?,?,?,?,?,?)",
            (item_id, ident.actor, kind, store.clip(body, store.MAX_ITEM_TEXT),
             run_id, now.isoformat(" ")))
        conn.commit()
        return 201, {"id": cur.lastrowid, "item_id": item_id,
                     "actor": ident.actor, "kind": kind,
                     "body": store.clip(body, store.MAX_ITEM_TEXT),
                     "run_id": run_id, "at": store._iso(now.isoformat(" "))}
    finally:
        conn.close()


@app.post("/items/{item_id}/checkout")
async def checkout_item(request: Request, item_id: str):
    """Check out a work item: claim it AND take the lease on the subtree it sits in.

    This is the protocol a run begins with. The lease is on the item's EPIC, so a
    second worker reaching for a sibling is refused with a reason naming the
    holder and the expiry. A correction, whose parent is its product's
    corrections queue, leases only itself: no lease is ever rooted on a queue.

    Returns `{item, lease, policy, adjacency}`. `policy` carries the grace in
    force, so a caller sees the terms it is held to rather than inferring them
    from a refusal — the grace is a tunable that moves, and a client that
    hardcoded it would drift the moment it does.

    **`adjacency` IS THE NEAR-DUPLICATE BRIEFING, AND IT ARRIVES WITH THE LOCK**
    rather than being offered as a separate search (contract §4, ARCH-424).
    Deduplication a caller has to remember to run is deduplication that will not
    happen. `available: false` means the briefing could not be computed, which is
    a different fact from an empty list — a worker told "nothing resembles this"
    on the strength of never having looked is the one answer this must never give
    by accident.

    **The briefing is computed AFTER the lock is taken and cannot refuse it.** By
    the time it runs the claim and the lease have committed, so a briefing that
    throws costs the caller its advice and not its hold.

    **Also returns the call slip's grant** (`slip_id`, `persona_md`, `docs`,
    `bundle`, `model`, `budget`, `limits` — ADR-0062), additive alongside the
    four fields above. An optional `persona` on the request names the kind of
    agent wanted; an absent one means exactly what checkout meant before this
    field existed. `slip_id` names a real, persisted credential with its own
    TTL independent of this checkout's lease — see `call_slips` for its
    lifetime and `_call_slip_grant` for what the rest of the grant's fields
    are still a placeholder for and why. `model` is resolved from the item's
    own model choice or `persona`'s agent definition, falling back to that
    same placeholder when neither names one (issue_01M12NE12WF9V0721WSHH34A2W,
    `_call_slip_grant_body`).

    **An optional `profile`, as `"<project>/<variant>"`, is what fills
    `docs`** (issue_01M21B12X6BYZ76W04JK2QWPQF). The caller names it because
    the Library cannot derive it: a product is not a profile project — the
    product `convivy-deck` and the profile project `deck` are different
    strings for one crew — and four projects run more than one variant over
    the same work items, so which one a dispatch is running is a fact only
    whoever launched it holds. Omitting it is not an error and yields the
    empty `docs` a checkout returned before the field existed.

    **A `profile` naming no existing row refuses the whole checkout with 400,
    before any lock is taken.** A caller that named a profile is asking for a
    bundle, so returning it an empty one over a live lease would let a
    dispatch individuate nothing and still report success. The refusal costs
    nothing: no lease was taken, and the item is still claimable on the retry.
    **The same 400 answers a `profile` naming a project/variant other than a
    bound identity's own** (issue_01M35QG0VANA5P1XE3PH6A7PS4): `profile` is
    named by the caller and not read from the credential, so without this a
    bound identity could fill `docs` from any other crew's document set
    through its own checkout.

    **A blocked item is checked out but not claimed** (the operator, 2026-09-12,
    platform/decisions/0079). The lease is taken so the owning crew's co can
    judge and correct the item under it, and the claim is not, so the item stays
    `open` and out of the claimable pool. The response carries `claimed: false`,
    the `unmet_count` and a hint, with no briefing and no call slip, since both
    are for a dispatch doing the work. A checkout that does claim carries
    `claimed: true`. `/release` hands a lease-only hold back.
    """
    return await _checkout(request, item_id, with_lease_body=True)


@app.post("/items/{item_id}/claim")
async def claim_item(request: Request, item_id: str):
    """Claim an item. Superseded by `/checkout`, and kept working deliberately.

    **THE OLD VERB TAKES THE LEASE TOO, and that is the whole point of sharing one
    implementation.** An actor still claiming the old way is precisely the hole in
    the fence: it takes work without a lease, so the librarian cannot see it
    holding anything and hands a sibling to someone else. A parallel `checkout`
    beside an unchanged `claim` would have built that hole deliberately and called
    it a migration path.

    The two differ in what they RETURN — `checkout` carries the lease, `claim`
    keeps the response shape its callers already parse — and in one case more:
    a blocked item. `/checkout` takes its lease without claiming it (the operator,
    2026-09-12), and `/claim` still refuses it 409, because a claim is a start
    on the work, and a blocked item's crew has ruled that work cannot start.
    """
    return await _checkout(request, item_id, with_lease_body=False)


async def _checkout(request: Request, item_id: str, *, with_lease_body: bool):
    ident, err = _identify(request)
    if err:
        return err
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    owner, err = _claim_owner(payload, ident)
    if err:
        return err
    run_id = payload.get("run_id")
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        return refusals.refuse(400, "run_id must be a non-empty string")
    run_id = store.clip(run_id.strip(), 100) if run_id else None

    # OPTIONAL, AND ADDITIVE (contract §1): checkout took no persona argument
    # before this field existed, and an absent one means exactly what checkout
    # already meant. There is no persona registry yet (issue
    # 01M12NE120JVQT4DV85N8MB7TP) — this names which kind of agent the caller
    # wants, for `_call_slip_grant` to echo back into `persona_md` and, since
    # issue_01M12NE12WF9V0721WSHH34A2W, to resolve `model` against.
    #
    # **VALIDATED ONLY ON THE `/checkout` PATH.** `/claim` never read `persona`
    # before this field existed, so a payload with a malformed one succeeded
    # there — contract §1 permits only ADDING within protocol version 1, and
    # refusing a request `/claim` used to accept is the version-2 move of
    # tightening what a request must contain.
    if with_lease_body:
        persona = payload.get("persona")
        if persona is not None and (not isinstance(persona, str) or not persona.strip()):
            return refusals.refuse(400, "persona must be a non-empty string")
        persona = store.clip(persona.strip(), store.MAX_IDENT) if persona else None
    else:
        persona = None

    # THE PROFILE THE DISPATCH RUNS UNDER, NAMED BY THE CALLER, because the
    # Library cannot derive it (issue_01M21B12X6BYZ76W04JK2QWPQF). Measured
    # against the live database on 2026-09-09 rather than argued:
    #
    # **`project` is not the item's `product`.** Six products carry no profile
    # project of that name — `bosun`, `convivy-deck`, `keel`, `shipwright`,
    # `substrate`, `svc` — and four profile projects have no items at all. The
    # near-miss is the instructive one: the product `convivy-deck` and the
    # profile project `deck` are different strings for the same crew, so a
    # derivation by equality would resolve nothing and look like a crew with an
    # empty bundle rather than like a bug.
    #
    # **And the variant is not derivable even given the project.** Four
    # projects carry more than one — `constance`, `hollis`, `sous-chef` and
    # `vivy` each run daytime and overnight over the same work items, two of
    # them commercial as well. Which one a dispatch runs under is a fact about
    # the dispatch, decided by whoever launched it, and nothing the Library
    # holds records it.
    #
    # OPTIONAL AND ADDITIVE (contract §1), the same shape `persona` above
    # takes: a checkout that names no profile behaves exactly as it did before
    # this field existed, and gets the empty `docs` it got before. Validated
    # only on the `/checkout` path for the same reason `persona` is — refusing
    # a payload `/claim` used to accept is the version-2 move of tightening
    # what a request must contain.
    if with_lease_body:
        profile = payload.get("profile")
        if profile is not None:
            if not isinstance(profile, str) or profile.count("/") != 1:
                return refusals.refuse(
                    400, "profile must be a string of the form "
                         "'<project>/<variant>'",
                    hint="the Library cannot derive it: a product is not a "
                         "profile project, and four projects run more than one "
                         "variant over the same work")
            project, _, variant = profile.partition("/")
            if not project.strip() or not variant.strip():
                return refusals.refuse(
                    400, "profile must name both a project and a variant")
            profile = (store.clip(project.strip(), store.MAX_IDENT),
                       store.clip(variant.strip(), store.MAX_IDENT))
    else:
        profile = None

    # BREAKING A LIVE LEASE IS A HUMAN-ONLY ACT, and this service issues no human
    # identity, so `force` is refused rather than ignored. Checked here rather
    # than in the domain layer so that layer stays policy-free and one route
    # cannot forget it — and an agent passing `force` gets told what it means
    # instead of silently having it dropped.
    if payload.get("force"):
        return refusals.refuse(
            403, "only a human may break a live lease",
            hint="a dead lease needs no force — retry without it; this service "
                 "issues no human identity until the Library mints its own "
                 "tokens")
    try:
        result = await run_in_threadpool(_do_checkout, ident, item_id, owner,
                                         run_id, with_lease_body, persona,
                                         profile)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    except call_slips.MintFailed as e:
        # THE DETAIL GOES TO THE LOG AND NOT TO THE CALLER, matching
        # `misconfigured_database`'s reasoning: `str(e)` may carry whatever the
        # underlying insert failure said, which is server-side material with no
        # reason to reach a caller holding an ordinary write capability.
        print(f"library: CALL SLIP MINT FAILED — {e}", file=sys.stderr)
        return refusals.refuse(
            503, "failed to mint a call slip",
            hint="the checkout took nothing — the claim and lease rolled back "
                 "with the failed mint, so retrying is safe. The detail is in "
                 "the service log.")
    status, body = result
    return JSONResponse(status_code=status, content=body)


def _amount(payload: dict, field: str):
    """A money amount from the wire: `(value, None)` or `(None, refusal)`.

    **ONE VALIDATOR FOR BOTH ROUTES, because they had the same three holes and
    only one of them had the cap** (reviewer and security-reviewer, PR #50).

    `NOT FINITE` IS THE HOLE THAT MATTERED. Starlette parses a body with the
    stdlib `json.loads`, which accepts the non-standard `Infinity`, `-Infinity`
    and `NaN` literals and hands back a Python float. `NaN < 0` is False and
    `inf < 0` is False, so both walked past a negativity check into
    `round()` — where `round(inf)` raises `OverflowError`, which is NOT a
    `ValueError` and was caught by nothing, so a single crafted body returned a
    bare 500. Verified rather than reasoned: `-Infinity` was in fact refused by
    the negativity check, and `+Infinity` and `NaN` were not.

    **THE CEILING IS THE SECURITY HALF, and it was missing from the spend route
    entirely** (security-reviewer). Spend deliberately never refuses on a cap,
    so an unbounded report is not merely a bad row: one call reporting an
    absurd amount drives a product's accumulated total past every threshold and
    puts every other agent on that product into the capped refusal until local
    midnight. `ledger.spend` is held by every agent-role credential, so that is
    a fleet-wide stop for one product from one request. A finite value can also
    exceed SQLite's 64-bit INTEGER on the way in and raise `OverflowError` from
    the INSERT itself, which is the 500 by another road.

    Bounded BEFORE the multiply, not after: `round(1e300 * 1_000_000)` is the
    overflow, so a check on the rounded value never runs.
    """
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, refusals.refuse(400, f"{field} must be a number")
    if not math.isfinite(value):
        return None, refusals.refuse(
            400, f"{field} must be a finite number",
            hint="JSON's Infinity and NaN are accepted by the parser and are "
                 "not amounts of money")
    if value < 0:
        return None, refusals.refuse(400, f"{field} must not be negative")
    if value > store.MAX_LEDGER_USD:
        return None, refusals.refuse(
            400, f"{field} is larger than this vault will record",
            hint="an amount this size is a typo or an attack; a real spend "
                 "that large needs a human to raise the ceiling",
            maximum_usd=store.MAX_LEDGER_USD)
    return value, None


def _opt(payload: dict, field: str):
    """A trimmed optional string, or None. One place, because a field that is
    validated in the route and then read raw at the call site is how a
    whitespace-only value reaches the store."""
    value = payload.get(field)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _within_a_day(day: str, today: str) -> bool:
    """Is `day` within one calendar day either side of `today`?

    **Compared as DATES rather than as strings.** A string comparison gets
    2026-03-01 and 2026-02-28 wrong at every month boundary, which is a
    twelve-times-a-year bug in a check whose whole job is to be unfoolable.
    """
    try:
        a = _dt.date.fromisoformat(day)
        b = _dt.date.fromisoformat(today)
    except ValueError:
        return False
    return abs((a - b).days) <= 1


def local_day() -> str:
    """Today, in the LOCAL timezone, as `YYYY-MM-DD`.

    **Local rather than UTC, matching `telemetry.platform_day()` on the other
    half of the Keel**, and the reason is recorded there rather than incidental:
    a UTC key rolls the day at 17:00 MST, folding an evening's interactive work
    into the next day's pool — which produced a false soft-cap crossing on
    2026-06-14. `agentctl` enforces the per-run cap and the Library refuses at
    checkout against the same daily budget, so the two halves bucketing
    differently would disagree about which day a spend falls in for seven hours
    out of every twenty-four.

    Timestamps everywhere else in this service stay UTC. Only the day BUCKET is
    local, which is the same split telemetry draws.
    """
    return _dt.datetime.now().astimezone().strftime("%Y-%m-%d")


def _refuse_on_capped_ledger(conn, product: str):
    """`(409, body)` when this product's ledger has a refusing cap whose
    threshold today's total has reached, or None when it does not — or when
    there is no ledger at all.

    **The total is `store.ledger_total`, which is never `subscription`-lane
    money (issue_01M35QG5NA13JE8FQVM9XET4HD).** A subscription estimate is
    not money a budget can be enforced against, so admission never refuses
    on it; a `gateway`-lane entry, and one with no `lane` at all, both do.

    **A MISSING LEDGER IS NOT A REFUSAL, and this is the load-bearing default.**
    Most projects are expected to stay uncapped, so failing closed here would
    stop the entire fleet the moment this ships. That is deliberately the
    opposite of the tool policy's posture, and the asymmetry is in what the
    failure costs: a policy that fails open grants access nobody approved,
    while a ledger that fails open lets work continue exactly as it already
    does.

    **Only the SOFT and HARD caps refuse here — the warning threshold never
    does.** `store.ledger_state` reports `warning` for information; this
    function refuses on `soft_capped` or `hard_capped` alone, matching the
    specification's "a soft cap refuses new grants... a hard cap revokes the
    tokens held by that project's active runs" — this route is the soft cap's
    half. A hard-capped total also refuses admission here, because Phase 5's
    revocation is not built yet (see `POST /ledger-entries`'s docstring for
    what that means and does not mean) and an unenforced hard cap that still
    let new work start would be a cap in name only.

    **The refusal names the numbers, because a refusal a caller cannot act on
    wastes the run twice.** The lease refusal is the worked example this
    follows — naming the holder and the expiry lets an orchestrator route
    elsewhere, where a bare "no" leaves it choosing between blocking and
    retrying. Here the actionable facts are which cap was hit, the total, and
    that the answer changes at midnight LOCAL rather than at some hour the
    caller has to infer.
    """
    ledger = store.live_ledger(conn, "project", product)
    if ledger is None:
        return None
    day = local_day()
    total = store.ledger_total(conn, ledger["id"], day)
    state = store.ledger_state(ledger, total)
    if state not in (store.LEDGER_STATE_SOFT_CAPPED, store.LEDGER_STATE_HARD_CAPPED):
        return None
    cap_name = "hard_cap" if state == store.LEDGER_STATE_HARD_CAPPED else "soft_cap"
    cap_micro = (ledger["hard_cap_usd_micro"] if state == store.LEDGER_STATE_HARD_CAPPED
                else ledger["soft_cap_usd_micro"])
    return 409, {
        "error": f"the ledger for {product!r} has reached its {cap_name} for {day}",
        "hint": "this is not a reason to try another item — every item on this "
                "product refuses the same way until the day rolls over "
                "(local midnight) or the cap is raised, which only a human "
                "may do through Orient",
        "ledger": {
            "id": ledger["id"], "scope_kind": "project", "scope_id": product,
            "day": day, "state": state,
            "cap": cap_name, "cap_usd": cap_micro / 1_000_000,
            "total_usd": total / 1_000_000,
        },
    }


# THE FALLBACK `model` VALUE, SERVED WHEN NOTHING RESOLVES ONE
# (issue_01M12NE12WF9V0721WSHH34A2W): no model choice on the work item, no
# persona named or none that resolves, an ambiguous persona name, or a
# reference index that cannot be read. Named so a reader who sees it in a
# run knows immediately that nothing chose it — a client that predates
# resolution sees exactly this value, unchanged.
_MODEL_NOT_RESOLVED = "model-resolution-not-implemented"


# WHY `persona_md` CAME BACK AS A PLACEHOLDER, served beside it as
# `persona_unresolved_reason` (issue_01M4XEEPQ37JFM3SE1493WDHVG).
# `PERSONA_UNRESOLVED` deliberately covers three cases a bound caller must not
# tell apart: no agent document of that name, an ambiguous name, and a
# document outside the caller's dispatch profile. Distinguishing them would
# say that a document of that name exists somewhere (security-reviewer,
# convivy/library#268 round 3).
PERSONA_NOT_NAMED = "no_persona"
PERSONA_INDEX_UNREADABLE = "index_unreadable"
PERSONA_UNRESOLVED = "unresolved"


def _persona_md(persona: str | None) -> str:
    """The placeholder `persona_md` carries when no agent definition resolves.

    **ITS TEXT IS A WIRE CONTRACT UNTIL THE BOSUN READS `persona_resolved`.**
    agentctl's interim fallback to the roster's definition fires on this exact
    text (issue_01M4XEHK9FRVRRMFGH6C80BC6T), so changing it would start an
    unresolved run on a placeholder as its agent definition. Why it did not
    resolve travels in `persona_unresolved_reason` instead.
    """
    heading = persona or "default"
    return (f"# {heading}\n\n"
            "Persona content is not resolved yet — no persona storage or "
            "registry exists. This is a placeholder pinning the call slip's "
            "shape ahead of it.")


def _resolve_persona(ident: identity.Identity,
                     persona: str | None) -> tuple[sqlite3.Row | None, str | None]:
    """`(row, None)` for the `kind: agent` document `persona` names, or
    `(None, reason)` with one of the `PERSONA_*` reasons above.

    **ONE LOOKUP FEEDS BOTH `model` AND `persona_md`**, so the grant can never
    pin one document's model beside another's definition.

    **Same id-suffix match and the same agent-platform preference `agentctl`'s
    `_library_agent` uses** (ARCH-426): every `kind: agent` document is read
    and matched on its id ending `/agents/<persona>`; several projects holding
    the name resolve to agent-platform's copy, and otherwise, or when nothing
    matches, the name is unresolved rather than refused. The two have to agree
    on one persona, because a caller that reads this grant's `model` and the
    `agentctl spawn --issue` dispatch that later runs the same persona must
    not be told two different models.

    **THE MATCH RUNS OVER EVERY AGENT DOCUMENT, UNNARROWED, AND ONLY THE
    RESULT IS THEN CHECKED AGAINST SCOPE** (issue_01M35QG0VANA5P1XE3PH6A7PS4):
    narrowing the candidate set to `ident`'s profile before the id-suffix
    match could change which copy the ambiguity tiebreak above prefers, which
    would disagree with `agentctl`'s own unscoped resolution. A bound identity
    whose persona resolves outside its profile gets exactly the answer an
    unresolved persona gets — the same "not yours to see" shape
    `_bound_scope_clause`'s other callers give a document outside scope.

    **A FAILURE HERE MUST NOT COST THE CALLER ITS CHECKOUT**, the same
    contract `_slip_docs` holds for a profile bundle: this returns a reason
    rather than raising.
    """
    if not persona:
        return None, PERSONA_NOT_NAMED
    try:
        conn = reference.connect()
    except reference.ReferenceIndexUnavailable as e:
        print(f"library: agent lookup unavailable for {persona!r} — {e}",
              file=sys.stderr)
        return None, PERSONA_INDEX_UNREADABLE
    try:
        rows = conn.execute("SELECT * FROM docs WHERE kind = 'agent'").fetchall()
        suffix = f"/agents/{persona}"
        matches = [r for r in rows if str(r["id"]).endswith(suffix)]
        if len(matches) > 1:
            matches = [r for r in matches if r["project"] == "agent-platform"]
        doc = matches[0] if len(matches) == 1 else None
        # A BOUND IDENTITY PAYS FOR THE SCOPE CHECK WHETHER OR NOT THE PERSONA
        # RESOLVED (security-reviewer, #268 round 3). Run only on a match, the
        # extra control-database read and query made "a document of this name
        # exists somewhere" measurable in latency, though the body was already
        # identical. An id no document carries stands in when nothing matched.
        scope = _bound_scope_clause(ident)
        if scope is not None:
            scope_clause, scope_params = scope
            in_scope = conn.execute(
                f"SELECT 1 FROM docs WHERE id = ? AND {scope_clause}",
                [doc["id"] if doc is not None else "", *scope_params]
            ).fetchone()
            if in_scope is None:
                return None, PERSONA_UNRESOLVED
        if doc is None:
            return None, PERSONA_UNRESOLVED
        return doc, None
    except (sqlite3.Error, FileNotFoundError) as e:
        print(f"library: agent lookup unavailable for {persona!r} — {e}",
              file=sys.stderr)
        return None, PERSONA_INDEX_UNREADABLE
    finally:
        conn.close()


def _persona_model(doc: sqlite3.Row | None) -> str | None:
    """The `model` frontmatter pin of a resolved agent definition, or `None`
    when there is no document or it pins none."""
    if doc is None:
        return None
    model = reference.doc_dict(doc, body=False).get("model")
    return model if isinstance(model, str) and model.strip() else None


def _call_slip_grant(ident: identity.Identity, persona: str | None, slip_id: str,
                     profile_row: dict | None = None,
                     per_run_cap_usd: float | None = None,
                     model_choice: str | None = None) -> dict:
    """`{slip_id, persona_md, docs, model, budget, limits}` — the grant a call
    slip carries (ADR-0062), added to `POST /items/{id}/checkout`'s response
    alongside the unchanged `item`, `lease`, `policy` and `adjacency`. This is
    additive within protocol version 1 (contract §1): every field below is
    new, and nothing already returned changed shape or meaning.

    **`limits` still pins a SHAPE rather than a resolved value**
    (issue_01M12NE120JVQT4DV85N8MB7TP). **`model` is resolved**
    (issue_01M12NE12WF9V0721WSHH34A2W): see `_call_slip_grant_body`'s
    docstring for the order it is resolved in. **`persona_md` is the body of
    the agent definition `persona` names** (issue_01M4XEEPQ37JFM3SE1493WDHVG),
    and `persona_resolved` says whether it is: when it is `false`,
    `persona_md` is `_persona_md`'s placeholder and `persona_unresolved_reason`
    says why. A client that predates these two fields sees neither, and a slip
    minted before them carries neither when redeemed, so a missing
    `persona_resolved` reads as `false`.

    **`slip_id` is the one field that is real.** `_do_checkout` mints and
    persists a row in `call_slips` before calling this, and that id is what
    `POST /tokens/redeem` will look up (issue_01M12NE13P3FEY3HF7732QX03F, a
    separate task — not built here). Serving a grant that named a slip
    without a store behind it was the thing PR #78 refused to do; this PR is
    that store, so the refusal no longer applies and the id is minted for
    real, with its own TTL (`call_slips.TTL_SECONDS`) independent of the
    checkout lock.

    **`docs` NAMES DOCUMENTS BY ID AND NEVER THEIR CONTENT, which is the
    context-economy property the whole epic is for.** Each entry is an `id`
    and a `body_sha256`, and nothing else: a caller's context stays flat
    however large the bundle grows, and the hash is what lets the Individuator
    notice a document that changed between this grant and its later fetch
    (issue_01M19QQCKXW818SKY5A4G3XPHT). Metadata a caller actually wants —
    title, kind, source_ref — is one `GET /reference/documents/{id}` away and
    does not belong on a lock's response.

    **It is populated only when the checkout NAMED a profile**
    (issue_01M21B12X6BYZ76W04JK2QWPQF), and empty otherwise. The comment here
    has been wrong once already in the direction of looking deliberate — it
    said there was no bundler when `crew_profiles.resolve` had just been
    built — so it states the mechanism rather than a status: `docs` is the
    resolution of the profile the caller named, and no profile named means
    nothing to resolve.

    **`bundle` says which profile produced `docs`, and whether it could be
    read at all.** `available: false` with an empty `docs` is a different fact
    from an empty bundle, and the distinction is the same one `adjacency`
    already draws: a worker told "your bundle is empty" on the strength of
    never having looked is the answer this must never give by accident. It
    goes false only when a profile was named, existed, and the knowledge index
    could not be opened to evaluate it.

    **`budget` carries a per-run cap and nothing else.** The Bosun's epic
    (issue_01M1HR62S6YVMR1G6RHQ8RB5Q2) settles that the slip bounds one run
    and carries no project total — the Library's ledger (§Chart House) holds
    that and evaluates its own thresholds, so a slip never goes stale as a
    project's spend moves. `cap_usd` is the checked-out item's project
    ledger row's `per_run_cap_usd_micro`, in dollars, a number only a human
    sets through `PUT /ledgers` (issue_01M4BDRV8C47NQN5X8T82Y2B9A) — `null`
    when the project has no ledger row or the row has no per-run cap. It is
    deliberately not the ledger's accumulating total and moves no threshold
    on it; `agentctl` reads `cap_usd` and enforces it in-process, including
    the kill, and the Library enforces nothing about it.

    **`limits` is reserved and empty.** `policy` already carries this
    checkout's lease terms (`grace_seconds`, `max_lease_seconds`) — the
    lock's lifecycle. `limits` is for the call slip's own run-level
    constraints, a different axis nothing here restates, and none of those
    constraints are decided yet.
    """
    return {"slip_id": slip_id,
            **_call_slip_grant_body(ident, persona, profile_row,
                                    per_run_cap_usd, model_choice)}


def _call_slip_grant_body(ident: identity.Identity, persona: str | None,
                          profile_row: dict | None,
                          per_run_cap_usd: float | None,
                          model_choice: str | None = None) -> dict:
    """Every field of `_call_slip_grant` but `slip_id`: what checkout stores
    on the slip at mint and what `POST /tokens/redeem` returns as `grant`.
    Built before the slip exists, so it cannot name the slip's own id.

    **`model` is resolved in this order** (issue_01M12NE12WF9V0721WSHH34A2W):
    `model_choice` — the work item's own `model_choice.model`, read by the
    caller off `issue_items` before this is called, since that table is not
    this function's to query — wins when set; otherwise `persona`'s agent
    definition's `model` pin, when `persona` names one that resolves AND
    falls inside `ident`'s bound dispatch profile (`_resolve_persona`,
    issue_01M35QG0VANA5P1XE3PH6A7PS4); otherwise `_MODEL_NOT_RESOLVED`,
    exactly what every caller saw before either source existed.

    **`_do_checkout`, this function's one caller, passes `model_choice=None`
    here and applies that half of the order itself, after the call, from
    `locked["model_choice"]`** — the row under the write lock, not the
    unlocked `item` this function would otherwise have no way to tell apart
    from a real absence of a choice. Passing the unlocked value in would
    resolve `model` before the lock is even taken, the same race the write
    lock exists to close."""
    docs, available = _slip_docs(profile_row)
    doc, unresolved = _resolve_persona(ident, persona)
    definition = doc["body"] if doc is not None else None
    if doc is not None and not (isinstance(definition, str) and definition.strip()):
        # A document with no body has nothing to render; serving it as
        # resolved would hand the agent an empty definition.
        definition, unresolved = None, PERSONA_UNRESOLVED
    if definition is not None and not identity.authorized(ident, "reference.read"):
        # **A DOCUMENT'S BODY NEEDS `reference.read` HERE TOO**, the capability
        # `GET /reference/documents/{id}` requires (security-reviewer,
        # convivy/library#283). Checkout needs only `item.claim`, and a
        # credential may withhold `reference.read` while keeping it, so without
        # this the grant would hand out what the document route refuses. The
        # caller gets the answer a persona nothing carries gets.
        definition, unresolved = None, PERSONA_UNRESOLVED
    model = model_choice or _persona_model(doc) or _MODEL_NOT_RESOLVED
    return {
        "persona_md": definition if definition is not None else _persona_md(persona),
        "persona_resolved": definition is not None,
        "persona_unresolved_reason": unresolved if definition is None else None,
        "docs": docs,
        "bundle": {
            "profile": (f"{profile_row['project']}/{profile_row['variant']}"
                        if profile_row else None),
            "available": available,
        },
        "model": model,
        "budget": {"cap_usd": per_run_cap_usd},
        "limits": {},
    }


def _per_run_cap_usd(conn, product: str) -> float | None:
    """The project's per-run cap in dollars, for `budget.cap_usd`. Same scope
    `_refuse_on_capped_ledger` checks, read separately because that call
    answers a refusal, not a row to carry forward. `None` when the project has
    no ledger row, which is the ordinary case for a project no human has
    capped, or when its row sets no per-run cap."""
    ledger = store.live_ledger(conn, "project", product)
    if ledger is None or ledger["per_run_cap_usd_micro"] is None:
        return None
    return ledger["per_run_cap_usd_micro"] / 1_000_000


def _slip_docs(profile_row: dict | None) -> tuple[list[dict], bool]:
    """`(docs, available)` for a grant — the profile resolved to `id` plus
    `body_sha256` per document.

    **A FAILURE HERE MUST NOT COST THE CALLER ITS CHECKOUT.** This runs before
    the lease is taken (`_do_checkout` builds the grant ahead of its first
    write, so the slip can store it), so a failure that propagated would no
    longer strand a hold; it would refuse every checkout naming a profile for
    as long as the reference index is being rebuilt, and the queue would stop
    on advice it can do without. This is the same trade `_briefing` makes for
    adjacency, and `available: false` is how the caller learns the difference
    rather than reading an empty list as an answer. The same `available` is
    stored with the slip, so redemption reports it unchanged.

    **THE GUARD SPANS THE QUERY AND NOT ONLY THE OPEN, because the index can
    fail in both windows and only one of them raises
    `ReferenceIndexUnavailable`** (reviewer, PR #101). `reference.connect()`
    converts every way it can fail to hand back a usable connection; a
    `SELECT` on the connection it returned raises `sqlite3.Error` instead,
    which nothing between here and the client catches — `_checkout` names only
    `store.WritesDisabled` and `call_slips.MintFailed`. Guarding the open
    alone left the exact stranded hold this function exists to prevent, moved
    one call later.

    The condition is named rather than imagined: `reference.connect()`'s own
    docstring records that the index file "can be removed or replaced in the
    window" around opening it, because the knowledge watcher rebuilds it in
    place. A rebuild landing mid-query is the same event as one landing
    mid-open, and `database disk image is malformed` is what the read raises
    when it does — a failure this repository's own test fixtures produced by
    accident while this PR was being written.

    **BOTH CATCHES LOG THE DETAIL TO STDERR, because catching here is what
    stops the registered handler from doing it** (security-reviewer, PR #101).
    `misconfigured_database` and `reference_index_unavailable` both print the
    exception before returning a sanitized refusal, on the standing reasoning
    that the caller gets a hint and the operator needs the real string
    somewhere. Handling the failure locally means neither handler ever runs,
    so an index left dead or corrupt would make every profile-naming checkout
    report `available: false` indefinitely with nothing in `serve.log` to
    grep for — the caller correctly informed and the operator unable to learn
    why. The same failure reached through any Reference Room route logs; this
    path has to as well, or it becomes the quiet one.
    """
    if profile_row is None:
        return [], True
    named = f"{profile_row['project']}/{profile_row['variant']}"
    try:
        index_conn = reference.connect()
    except reference.ReferenceIndexUnavailable as e:
        print(f"library: profile bundle unavailable for {named} — {e}",
              file=sys.stderr)
        return [], False
    try:
        rows = crew_profiles.resolve(profile_row, index_conn)
    except sqlite3.Error as e:
        print(f"library: profile bundle unavailable for {named} — {e}",
              file=sys.stderr)
        return [], False
    finally:
        index_conn.close()
    return [{"id": r["id"], "body_sha256": r["body_hash"]} for r in rows], True


def _no_such_checkout_profile(project: str, variant: str) -> tuple[int, dict]:
    """The refusal `/items/{id}/checkout` gives for a `<project>/<variant>`
    it cannot resolve — a profile the `profiles` table does not hold, AND
    (issue_01M35QG0VANA5P1XE3PH6A7PS4) a profile a bound identity holds but
    did not name its own. Shared between the two so the body is byte for
    byte identical either way, and a bound caller learns nothing about which
    other profiles exist."""
    return 400, {
        "error": f"no profile named {project}/{variant}",
        "hint": "GET /profiles lists the profiles that exist; a checkout "
                "naming none omits `profile` and gets an empty bundle",
    }


def _do_checkout(ident, item_id: str, owner: str, run_id, with_lease_body: bool,
                 persona: str | None = None,
                 profile: tuple[str, str] | None = None):
    # THE VECTOR-AWARE HANDLE, WHEN ONE ATTACHES, because the briefing runs a KNN
    # over `vec0` on this same connection once the lock has committed. A plain
    # `store.connect_rw` reports `no such module: vec0` there.
    #
    # **`/claim` PAYS THE EXTENSION LOAD TOO, and that is the deliberate half of
    # the trade** (reviewer, PR #19). The KNN is skipped on that path, but the
    # load is not — one shared library open per checkout, against branching the
    # connection on a flag and giving the two verbs different handles. The second
    # would put "which connection am I on" into a function that already has
    # enough to keep straight.
    #
    # **A missing or unattachable vectors file falls back to a plain
    # `store.connect_rw()` instead of taking `/checkout` and `/claim` down with
    # it** (reviewer + security-reviewer, issue_01M35R5GA1SKRG2CMQRF189NDE
    # round 1) — the queue's own admission mechanism does not depend on the
    # index, and `_briefing` below reports `vectors_reason` instead of running a
    # query that would fail anyway.
    conn, vectors_reason = vectors.connect_rw_if_available()
    if conn is None:
        conn = store.connect_rw()
    try:
        item = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                            (item_id,)).fetchone()
        if item is None or not identity.in_scope(ident, item["product"]):
            return 404, {"error": "no such item"}
        if not identity.authorized(ident, "item.claim"):
            return 403, {"error": "this identity lacks 'item.claim'"}

        # A CHECKOUT THAT NAMES A PROFILE RECONCILES THE PROFILES TABLE FIRST,
        # and only after the scope and capability refusals above, so a caller
        # that is refused cannot make the Library write. The reload uses its own
        # connection, and this one has issued only autocommit reads, so it holds
        # no lock the reload's writes wait on. The profile read below sees what
        # the reload committed.
        if profile is not None:
            profile_sync.refresh()

        # THE LEDGER REFUSAL — the last of the three hard grounds the Keel's
        # shape names for admission, alongside scope and capability. Releasing
        # and heartbeating deliberately do NOT get this check: refusing to let
        # go of work, or to say you are still alive, because a budget ran out
        # would strand the lease it was trying to release.
        #
        # **IT COMES BEFORE EVERY ITEM-SPECIFIC CHECK BELOW, and the order is
        # the design.** Every other refusal here means "try a different item":
        # an orchestrator told `already claimed` routes to the next one and
        # keeps going. A capped ledger means "stop", because no item on this
        # product is claimable and walking the queue would spend one request per
        # item to learn it twenty more times. The refusal that ends a run has to
        # arrive before the refusals that merely redirect it.
        #
        # **RESOLVED TO ITS PROJECT HERE, ONCE** — `issue_items.product` is
        # not normalized (an item may still be filed under a repository
        # name), while a ledger's scope now always is, so looking the item
        # up by its raw `product` would silently miss a cap on the project
        # that owns it. `_per_run_cap_usd` below reuses this same resolved
        # value for the same reason; `item["product"]` itself stays
        # unresolved everywhere else in this function — the call slip's own
        # scope (`call_slips.mint`'s `product=`) is a different question,
        # answered by whatever the item itself is filed under.
        #
        # **AN UNREADABLE REGISTRY FALLS BACK TO THE RAW `product`, NOT TO A
        # REFUSAL.** `store.ledger_scope_name` fails open the same way
        # `projects.resolve` does: admission's own load-bearing default is
        # that a missing ledger never refuses (see `_refuse_on_capped_ledger`
        # above), so a checkout must not start failing closed just because
        # the registry it would resolve through cannot be read — that would
        # turn an unrelated file-read fault into a fleet-wide admission
        # outage. Checkout keeps today's behaviour then: it reads whatever
        # ledger the raw string happens to name, exactly as it did before
        # this resolution existed.
        ledger_product = store.ledger_scope_name(item["product"])
        capped = _refuse_on_capped_ledger(conn, ledger_product)
        if capped:
            return capped

        # A PROFILE THAT DOES NOT EXIST REFUSES THE CHECKOUT, BEFORE THE LOCK.
        # The alternative — take the lease and hand back an empty `docs` — is
        # the silent shape this whole epic exists to remove: a dispatch would
        # individuate nothing, report success, and look exactly like a crew
        # whose profile legitimately selects little. A caller that named a
        # profile is asking for a bundle, so failing to find one is its error
        # to see and fix, and it costs nothing to retry because no lease was
        # taken.
        #
        # Placed here for the same reason the ledger refusal above is: it is a
        # ground for refusing the whole request, so it belongs with the other
        # grounds rather than after the mutation it would have to undo.
        if profile is not None:
            # A BOUND IDENTITY'S CALL-SLIP GRANT MAY ONLY RESOLVE ITS OWN
            # PROFILE (issue_01M35QG0VANA5P1XE3PH6A7PS4): `profile` here is
            # named by the caller, not read from `ident`, so without this a
            # bound credential could check out an item under any other
            # profile and have `docs` filled from its document set — the
            # same bypass this issue closes on `POST /bundles`. Checked
            # before the lookup, so the refusal is identical whether the
            # named profile belongs to another crew or does not exist at
            # all, and a bound caller learns nothing about which other
            # profiles exist.
            if _bound_profile_mismatch(ident, *profile):
                return _no_such_checkout_profile(*profile)
            profile_row = crew_profiles.profile_row(conn, *profile)
            if profile_row is None:
                return _no_such_checkout_profile(*profile)
        else:
            profile_row = None

        # AN AGENT MAY NOT CLAIM OPERATOR WORK, enforced here and not only in a
        # listing filter. Excluding `operator` from the claimable list protects
        # the drain's normal list-then-claim flow, but that is a filter on ONE
        # query rather than a rule: a caller holding the id from anywhere else
        # reaches this endpoint directly and the filter never runs. The result
        # would be the exact failure the field exists to prevent — an item
        # claimed by something that cannot finish it, with no outcome, invisible
        # for a day.
        #
        # 409 rather than 403: the caller is authorized, and the item's nature is
        # what refuses.
        if item["needs"] == store.ITEM_NEEDS_OPERATOR:
            return 409, {"error": "operator items are not agent-claimable; no "
                                  "agent can finish this one"}

        # A BLOCKED ITEM REFUSES A CLAIM HERE TOO, NOT ONLY IN THE LISTING
        # FILTER — the same reasoning the operator-needs check just above states
        # for itself: a caller holding the id from anywhere else reaches this
        # route directly, and `state=claimable`'s exclusion never runs for it.
        # **THE COUNT IS WHAT THE REFUSAL NAMES, NOT WHICH PREREQUISITE** —
        # an out-of-scope prerequisite's state or title must not leak through
        # a refusal any more than through the rendered field itself.
        #
        # **`/checkout` IS NOT REFUSED HERE** (the operator, 2026-09-12,
        # platform/decisions/0079): it takes the lease and leaves the claim,
        # decided below once `lease.take()` holds the write lock.
        unmet = store.unmet_prerequisite_count(conn, item)
        if unmet and not with_lease_body:
            return 409, {
                "error": "this item has unmet prerequisites",
                "unmet_count": unmet,
                "hint": "GET /items/{id} shows what is waited on, for the "
                        "prerequisites this identity may read"}

        # An open item is claimable outright. A claimed one is claimable only once
        # its claim has expired — the takeover path for a session that died
        # without releasing. Every other state is past the claim stage.
        note = None
        if item["state"] == store.ITEM_STATE_CLAIMED:
            expired, _ = lease.claim_status(conn, item)
            if not expired:
                return 409, {"error": f"already {item['state']}"}
            if item["claimed_by"] != owner:
                note = f"claim taken over from {item['claimed_by']} (expired)"
        elif item["state"] != store.ITEM_STATE_OPEN:
            return 409, {"error": f"already {item['state']}"}

        # **THE GRANT IS BUILT HERE, BEFORE THE FIRST WRITE, AND STORED WITH THE
        # SLIP** (issue_01M3VN8WV1K891BT866K82ZBPX). `POST /tokens/redeem`
        # returns the grant a slip was issued with, and the persona and profile
        # it is built from are named on this request and stored nowhere, so the
        # slip row has to carry it. Built before `lease.take` because this
        # transaction is deferred: no write lock is held yet, so resolving the
        # profile against the reference index holds up no other writer. The
        # checkout response serves this same dict, so what checkout told the
        # Bosun and what redemption tells the Individuator cannot differ.
        # `budget.cap_usd` is left for the mint below, which reads it under the
        # write lock, and so is `model`'s `model_choice` half: `model_choice`
        # is left `None` here so this call falls back to the persona's pin or
        # the placeholder, and the mint below overwrites `grant["model"]` from
        # `locked["model_choice"]` the same way it overwrites `cap_usd`
        # (security-reviewer, convivy/library#249). The persona lookup still
        # happens here, unlocked, because it is a reference-index read and
        # holds up no other writer either.
        grant = None
        if with_lease_body:
            grant = _call_slip_grant_body(ident, persona, profile_row, None, None)

        # THE LEASE IS TAKEN BEFORE THE CLAIM IS WRITTEN, so a refused checkout
        # leaves the item exactly as it found it. The other way round, a caller
        # blocked by a live lease would still have flipped the item to `claimed`
        # on its way to being told no.
        try:
            held = lease.take(conn, item, holder=owner, actor=ident.actor,
                              run_id=run_id, holder_crew=ident.crew,
                              crew_exempt=ident.kind == "approved_client")
        except lease.LeaseConflict as conflict:
            # RECORD THE COLLISION THE FENCE CAUGHT, before the response, because
            # a refusal leaves no other trace anywhere.
            lease.record_refusal(conn, item, conflict, requester=owner,
                                 actor=ident.actor)
            conn.commit()
            # Through the SAME helper the other two disclosures use. This site
            # had the check first (PR #7) and kept its own copy of it when the
            # helper was extracted, which left two implementations of one rule to
            # keep in sync by hand and put this site outside the structural guard
            # entirely — a fault injection here reintroduced the exact bug PR #7
            # closed with every test still green (reviewer, PR #12).
            return 409, _lease_refusal(
                conflict, visible=_lease_root_visible(conn, ident, conflict.lease))
        except lease.CrewMismatch:
            # **A LIVE LEASE WHOSE `holder` STRING MATCHES `owner` BUT IS
            # RECORDED AS A DIFFERENT CREW'S** (CQ·10) — the one case
            # `lease.LeaseConflict` above cannot catch, because `lease.take`'s
            # own conflict scan treats a matching `holder` string as this
            # caller's own. Nothing was written: the raise happens before
            # `take`'s insert or update, under the same lock, so a plain
            # rollback is enough.
            conn.rollback()
            return 409, _crew_mismatch_refusal()
        except lease.SubtreeTooLarge as too_big:
            # The fence could not be enumerated, so its extent is unknown.
            # Refusing is the only safe answer: proceeding would take a lease
            # whose reach nobody measured, and the uncounted items would stay
            # claimable while their epic is held — failing open, silently.
            conn.rollback()
            return 409, {"error": f"cannot enumerate this subtree: {too_big}",
                         "hint": "the tree beneath this item is malformed or far "
                                 "larger than any real epic; investigate before "
                                 "checking anything out beneath it"}

        # **A BLOCKED ITEM IS CHECKED OUT WITHOUT BEING CLAIMED** (the operator,
        # 2026-09-12, platform/decisions/0079). Judging a prerequisite is the
        # owning crew's co's call, and correcting the item is a write that
        # platform/decisions/0076 puts under the writer's own lease. Refusing the
        # checkout left that co no way to take one: on 2026-09-12 Deck, vivy and
        # the Bosun each wrote to their own blocked items with no lease held. So
        # the lease is taken and the claim is not. The item stays `open`, stays
        # out of the claimable pool, and no drain can start work on it.
        #
        # **DECIDED ON THE ROW AS IT IS UNDER THE WRITE LOCK**, which
        # `lease.take()` has just taken, rather than on the unlocked read above.
        # A prerequisite a concurrent `/correct` or verdict adds or clears in
        # between is seen, so a checkout racing a new block ends lease-only
        # instead of refused, and one racing an unblock goes on to claim.
        #
        # No call slip is minted and no briefing is computed: both exist for a
        # dispatch doing the work, and a blocked item is not ready for one.
        if with_lease_body:
            locked = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                                  (item_id,)).fetchone()
            if locked is None or locked["state"] != item["state"]:
                conn.rollback()
                return 409, {
                    "error": f"the item is no longer {item['state']}"
                             + (f"; it is now {locked['state']}" if locked else ""),
                    "hint": "re-read the item and retry against its current state"}
            unmet = store.unmet_prerequisite_count(conn, locked)
            if unmet:
                if locked["state"] == store.ITEM_STATE_CLAIMED:
                    # **ONLY AN EXPIRED CLAIM REACHES HERE**, since a live one
                    # was refused above, and a blocked item cannot be claimed.
                    # So the takeover hands the dead claim BACK rather than
                    # passing it on: the item returns to `open` with the event
                    # the takeover note would have carried. Leaving
                    # `claimed_by` on the dead holder gave the new holder a
                    # lease that `/release` refused as "claim held by" someone
                    # else, so it could only lapse on its grace period
                    # (reviewer, convivy/library#113).
                    lease.release_claim(
                        conn, item_id, actor=ident.actor,
                        note=f"expired claim by {locked['claimed_by']} handed "
                             f"back on a lease-only checkout of a blocked item",
                        prev_owner=locked["claimed_by"])
                    locked = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                                          (item_id,)).fetchone()
                conn.commit()
                held = conn.execute("SELECT * FROM issue_leases WHERE id = ?",
                                    (held["id"],)).fetchone()
                return 200, {
                    "item": store.item_dict(
                        locked, claim=lease.claim_status(conn, locked),
                        prerequisites=store.prerequisite_status(
                            conn, locked, in_scope=_scope_predicate(ident))),
                    "lease": lease.lease_dict(held, conn=conn),
                    "policy": lease.policy(),
                    "claimed": False,
                    "unmet_count": unmet,
                    "hint": "held but not claimed, because the item has unmet "
                            "prerequisites: judge or correct it under this "
                            "lease and release it, or check out again once it "
                            "is unblocked to claim it"}

        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        prev = item["state"]
        # **CONDITIONAL ON THE STATE THIS REQUEST READ, the same compare-and-swap
        # resolve/link-pr already carry** (reviewer, security-reviewer,
        # PR #81). The SELECT above takes no lock and the claimability checks are
        # Python-only, so a concurrent `/resolve` committing in this window used
        # to be silently overwritten by this UPDATE — reopening and reclaiming a
        # just-finished item, with the event below recording `from_state` as this
        # request's stale read rather than the item's real prior state. The loser
        # rolls back — no claim, no lease, no event — and is told the state it
        # collided with.
        # **THE BLOCKED CHECK ABOVE IS THE FAST, EXPLANATORY REFUSAL — THIS
        # UPDATE'S `WHERE` IS THE ONE THAT ACTUALLY CLOSES THE RACE**
        # (security-reviewer). The early `unmet_prerequisite_count` reads an
        # unlocked row; `lease.take()` above is what acquires the write lock
        # (`BEGIN IMMEDIATE`), and a concurrent `/correct` or
        # `/prerequisites/verdict` adding or flipping a blocking entry could
        # commit in the window between that early read and this statement.
        # Folding `blocked_clause()` — the same rule the claimable listing
        # filters on — into this UPDATE's `WHERE` makes the claim itself, not
        # merely the check before it, refuse a blocked item: the clause is
        # evaluated against the row as SQLite sees it at this statement, under
        # the write lock, so nothing committed after can be missed.
        blocked_sql, blocked_params = store.blocked_clause()
        cur = conn.execute(
            "UPDATE issue_items SET state = ?, claimed_by = ?, claimed_at = ?, "
            "claimed_by_crew = ? WHERE id = ? AND state = ? AND NOT " + blocked_sql,
            (store.ITEM_STATE_CLAIMED, owner, now.isoformat(" "), ident.crew,
             item_id, prev, *blocked_params))
        if cur.rowcount != 1:
            conn.rollback()
            fresh = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                                 (item_id,)).fetchone()
            if fresh is None:
                return 404, {"error": "no such item"}
            if fresh["state"] == prev:
                # id and state both matched; the `WHERE` missed only because
                # `NOT blocked_sql` failed — a concurrent write blocked the
                # item after the early check ran. Same refusal that check
                # gives, recomputed against the row this UPDATE actually saw.
                unmet = store.unmet_prerequisite_count(conn, fresh)
                return 409, {
                    "error": "this item has unmet prerequisites",
                    "unmet_count": unmet,
                    "hint": "GET /items/{id} shows what is waited on, for the "
                            "prerequisites this identity may read"}
            return 409, {
                "error": f"the item is no longer {prev}; it is now "
                         f"{fresh['state']}",
                "hint": "re-read the item and retry against its current state"}
        conn.execute(
            "INSERT INTO issue_events (item_id, actor, from_state, to_state, at, "
            "note) VALUES (?,?,?,?,?,?)",
            (item_id, ident.actor, prev, store.ITEM_STATE_CLAIMED,
             now.isoformat(" "), note))
        slip_id = None
        if with_lease_body:
            # MINTED IN THE SAME TRANSACTION AS THE CLAIM AND THE LEASE,
            # deliberately unlike the briefing below. A checkout whose slip
            # fails to insert is not a degraded checkout, it is an unusable
            # one: the caller would hold the fence, be unable to retry (the
            # item is claimed by itself), and have no credential for the
            # thing the slip exists to do. The briefing degrades because
            # advice is optional; a credential is not. So the mint is atomic
            # with the claim — either the caller gets a usable checkout or
            # nothing, and a failed mint is cleanly retryable because no lock
            # was taken.
            # TRANSLATED TO A TYPED FAILURE HERE, still inside the transaction
            # and before `conn.commit()`, so raising costs the claim and lease
            # nothing — they roll back with the failed insert. The translation
            # is what lets `_checkout` answer with a named, retry-safe refusal
            # instead of an unhandled 500 (reviewer, PR #86).
            product = item["product"]
            # **THE PER-RUN CAP IS READ HERE, UNDER THE WRITE LOCK**
            # (reviewer, convivy/library#244). `agentctl` enforces it,
            # including the kill, and the slip keeps it for redemption, so it
            # is read where no `PUT /ledgers` can commit between the read and
            # the mint. The reads before `lease.take()` are autocommit, each
            # its own snapshot, so reading it there could store a cap a human
            # had already changed. `ledger_product` (resolved above, before
            # the capped-ledger refusal) is reused rather than resolving
            # `product` a second time — the two are the same item, read
            # before the same lock.
            grant["budget"]["cap_usd"] = _per_run_cap_usd(conn, ledger_product)
            # **`model_choice` IS RE-READ HERE TOO, OFF `locked`, UNDER THE
            # SAME WRITE LOCK** (security-reviewer, convivy/library#249).
            # `locked` is the row `lease.take()` already holds the lock on
            # (read above for the blocked-item check), so a `PUT
            # /items/{id}/model` landing in the window before `lease.take()`
            # is not missed the way the unlocked `item["model_choice"]` read
            # before the grant was first built would miss it. An unset
            # `model_choice` on `locked` leaves `grant["model"]` exactly as
            # `_call_slip_grant_body` resolved it above, from the persona's
            # pin or the placeholder.
            if locked["model_choice"]:
                grant["model"] = locked["model_choice"]
            try:
                slip_id = call_slips.mint(conn, item_id=item_id,
                                          product=product,
                                          claim_owner_label=owner,
                                          grant=grant)
            except Exception as e:
                raise call_slips.MintFailed(str(e)) from e
        conn.commit()
        fresh = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                             (item_id,)).fetchone()
        if not with_lease_body:
            # **NO BRIEFING ON `/claim`, DELIBERATELY.** The old verb keeps the
            # response shape its callers already parse, and computing a briefing
            # nothing in that shape can carry would be paying for advice no caller
            # can read. `/checkout` is where the briefing lives.
            return 200, store.item_dict(
                fresh, claim=lease.claim_status(conn, fresh),
                prerequisites=store.prerequisite_status(
                    conn, fresh, in_scope=_scope_predicate(ident)))
        held = conn.execute("SELECT * FROM issue_leases WHERE id = ?",
                            (held["id"],)).fetchone()
        # AFTER THE COMMIT. The lock is already the caller's, so the briefing is
        # reading rather than deciding, and it holds no write lock while it runs.
        return 200, {"item": store.item_dict(
                         fresh, claim=lease.claim_status(conn, fresh),
                         prerequisites=store.prerequisite_status(
                             conn, fresh, in_scope=_scope_predicate(ident))),
                     "lease": lease.lease_dict(held, conn=conn),
                     "policy": lease.policy(),
                     "claimed": True,
                     "adjacency": _briefing(conn, fresh, ident,
                                            vectors_unavailable=vectors_reason),
                     "slip_id": slip_id, **grant}
    finally:
        conn.close()


# --- POST /tokens/redeem: the call slip's counterpart (ADR-0062) ---------------

@app.post("/tokens/redeem")
async def redeem_token(request: Request):
    """Exchange a call slip for a library card (ADR-0062, contract §7).

    **Refuses unless `credential_binding.redemption_binding_holds` says the
    connection's authenticated machine is the one this credential is bound
    to** (ADR-0062's transport actor cross-check; the one call below, on
    every request, with no flag, header or role that skips it). A request on
    the plain listener has no authenticated machine and is refused with a
    403; every other failure is one 401 that names neither the machine the
    connection proved nor the one the credential is bound to, so a caller
    cannot use it to learn what is bound where. The check runs before the
    body is read and before any slip lookup, so a refusal reveals nothing
    about slips either. ADR-0062's Bosun signature binding is not checked:
    the operator cancelled it on 2026-09-04 (`convivy/knowledge#339`); the control
    moved to the Individuator's authentication to the Bosun.

    Past that gate: `slip_id` is looked up, scoped to the caller's product
    (404 covers both "no such slip" and "not yours to see", matching every
    other route), refused if expired (`call_slips.is_redeemable`), and
    consumed by a conditional `UPDATE` (`call_slips.redeem`) rather than a
    read-then-write — two concurrent redemptions of one slip must produce
    exactly one card, and the database decides the winner. The card is
    issued in the same transaction as the consuming update, so a slip is
    never left consumed with no card behind it and never redeemed twice.

    **The card's `actor` is `ident.actor` — the identity this request itself
    authenticated — never `claim_owner_label` off the slip.** See
    `library_cards.issue`'s docstring for why, which is
    issue_01M12NE13P3FEY3HF7732QX03F's answer to comments 1699 and 1700: the
    label is a caller-supplied coordination string, not an authorization
    principal, and reaching for it here would compile, run, and check
    nothing.

    **The card pins `Library-Protocol` at redemption**, read from
    `request.state.protocol_version` — set by `negotiate_version` before any
    route runs — for the card's lifetime (contract §1).
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "token.redeem"):
        return refusals.forbidden("token.redeem")
    if mtls.EXTENSION_KEY not in request.scope.get("extensions", {}):
        return refusals.refuse(
            403, "token redemption is served only over the mutual-TLS listener",
            hint="present a registered client certificate to "
                 f"{mtls.HOST}:{mtls.TLS_PORT}; the plain listener "
                 "authenticates no machine")
    if not credential_binding.redemption_binding_holds(request.scope):
        return refusals.refuse(
            401, "this credential is not bound to the presenting machine",
            hint="redeem from the machine this credential is bound to")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    slip_id = payload.get("slip_id")
    if not isinstance(slip_id, str) or not slip_id.strip():
        return refusals.refuse(400, "slip_id must be a non-empty string")
    slip_id = store.clip(slip_id.strip(), store.MAX_IDENT)
    try:
        result = await run_in_threadpool(
            _do_redeem, ident, slip_id, request.state.protocol_version)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    except library_cards.CardIssueFailed as e:
        print(f"library: CARD ISSUE FAILED — {e}", file=sys.stderr)
        return refusals.refuse(
            503, "failed to issue a library card",
            hint="the redemption took nothing — the slip was not consumed, "
                 "so retrying is safe. The detail is in the service log.")
    status, body = result
    return JSONResponse(status_code=status, content=body)


def _do_redeem(ident, slip_id: str, protocol_version: int):
    conn = store.connect_rw()
    try:
        slip = call_slips.get(conn, slip_id)
        if slip is None or not identity.in_scope(ident, slip["product"]):
            return 404, {"error": "no such call slip"}
        if not call_slips.is_redeemable(slip):
            return 409, {"error": "this call slip has expired",
                        "hint": "a call slip is minted fresh at every "
                                "checkout; check the item out again for a "
                                "new one"}
        redeemed = call_slips.redeem(conn, slip_id)
        if redeemed is None:
            conn.rollback()
            return 409, {"error": "this call slip has already been redeemed"}
        try:
            card_id = library_cards.issue(
                conn, slip=redeemed, actor=ident.actor,
                protocol_version=protocol_version)
        except Exception as e:
            raise library_cards.CardIssueFailed(str(e)) from e
        conn.commit()
        card = library_cards.get(conn, card_id)
        # **THE GRANT THE SLIP WAS ISSUED WITH, beside the card**
        # (issue_01M3VN8WV1K891BT866K82ZBPX): persona, documents by id and
        # hash, model, budget and limits, so the Individuator renders the agent
        # from this answer alone. Additive within protocol version 1: the
        # card's fields are unchanged and `grant` is new.
        grant = call_slips.grant_of(redeemed)
        # **THE REDEEMER NEEDS `reference.read` FOR THE DEFINITION'S BODY**
        # (security-reviewer, convivy/library#283), as the checkout caller did
        # when it was minted: a slip may be redeemed by another credential,
        # gated on `token.redeem` alone. Without it the body is replaced by the
        # placeholder, under the default heading because the slip does not
        # keep the persona's name.
        if (grant and grant.get("persona_resolved")
                and not identity.authorized(ident, "reference.read")):
            grant = {**grant, "persona_md": _persona_md(None),
                     "persona_resolved": False,
                     "persona_unresolved_reason": PERSONA_UNRESOLVED}
        return 200, {**library_cards.card_dict(card), "grant": grant}
    finally:
        conn.close()


# --- POST /bundles, POST /bundles/redeem: the individuation bundle -----------
#
# The Library's side of `platform/decisions/0085`. `bundles.py`'s module
# docstring is the design record; this is the wire. Both routes are additive
# within `Library-Protocol: 1`, so a client that predates them sees a 404 for
# each and nothing else changes.


def _bundle_refusal(e: "bundles.BundleRefused") -> JSONResponse:
    return refusals.refuse(e.status, e.error, hint=e.hint, **e.fields)


def _bounded_name(payload: dict, field: str, *, required: bool):
    """`payload[field]` as a trimmed identifier-shaped string, `None` when it
    is absent and not required, or a 400 refusal. Never a clipped value: a
    name cut to fit is a different name, and the caller would get a bundle for
    something it did not ask for."""
    value = payload.get(field)
    if value is None:
        if required:
            return None, refusals.refuse(400, f"{field} is required")
        return None, None
    if not isinstance(value, str) or not value.strip():
        return None, refusals.refuse(400, f"{field} must be a non-empty string")
    value = value.strip()
    if len(value) > store.MAX_IDENT:
        return None, refusals.refuse(
            400, f"{field} is longer than {store.MAX_IDENT} characters")
    return value, None


@app.post("/bundles")
async def issue_bundle(request: Request):
    """Issue a token naming the documents one agent needs for one task.

    **Body:** `{"agent": <roster name>, "item_id": <id>}` or `{"agent": ...,
    "repo": <repository>}`, and optionally `"variant"` (default `daytime`).
    Exactly one of `item_id` and `repo`: both would leave it undefined which
    one scopes the bundle, and the Bosun already knows which it has.

    **201:** `{"token_id", "agent", "scope": {"kind", "item_id" | "repo",
    "project", "variant"}, "documents": [...], "count", "expires_at"}`. Each
    document is `reference.doc_dict`'s shape without a body, so it carries `id`
    and `body_sha256`. `token_id` is redeemable once, before `expires_at`, at
    `POST /bundles/redeem`.

    **Refusals, each naming what it found.** 400 for a malformed request. 404
    for an item that does not exist or is not the caller's to see, one wording
    for both. 422 when the request names something that does not exist: an
    agent that is not on the roster, or a `<project>/<variant>` with no
    profile. **A bound identity naming a project/variant other than its own
    gets the same 422** (issue_01M35QG0VANA5P1XE3PH6A7PS4): the agent role
    carries `bundle.issue`, and without this a bound credential could issue a
    bundle under any other profile and read its documents, the exact bypass
    the binding exists to close. 409 when the Reference Room cannot honour
    it: a document the profile selects is not `current`, an audience filter
    matches nothing, or the agent's definition is absent. **None of the 409s
    degrades to a smaller bundle**; see `bundles.py`.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "bundle.issue"):
        return refusals.forbidden("bundle.issue")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    agent, err = _bounded_name(payload, "agent", required=True)
    if err:
        return err
    item_id, err = _bounded_name(payload, "item_id", required=False)
    if err:
        return err
    repo, err = _bounded_name(payload, "repo", required=False)
    if err:
        return err
    variant, err = _bounded_name(payload, "variant", required=False)
    if err:
        return err
    if (item_id is None) == (repo is None):
        return refusals.refuse(
            400, "name exactly one of item_id and repo",
            hint="a bundle is scoped to an (agent, work item) pair, or to an "
                 "(agent, repository) pair when a dispatch names no item")
    try:
        status, body = await run_in_threadpool(
            _do_issue_bundle, ident, agent, item_id, repo,
            variant or bundles.DEFAULT_VARIANT)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    except bundles.BundleRefused as e:
        return _bundle_refusal(e)
    except bundles.MintFailed as e:
        print(f"library: BUNDLE TOKEN MINT FAILED — {e}", file=sys.stderr)
        return refusals.refuse(
            503, "failed to issue a bundle token",
            hint="nothing was committed, so retrying is safe. The detail is "
                 "in the service log.")
    return JSONResponse(status_code=status, content=body)


def _no_such_profile(project: str, variant: str) -> "bundles.BundleRefused":
    """The refusal `POST /bundles` gives for a `<project>/<variant>` it
    cannot issue against — a profile the `profiles` table does not hold, AND
    (issue_01M35QG0VANA5P1XE3PH6A7PS4) a profile a bound identity holds but
    did not name its own. Shared between the two so the body is byte for
    byte identical either way, and a bound caller learns nothing about which
    other profiles exist."""
    return bundles.BundleRefused(
        422, f"no profile {project}/{variant}",
        hint="GET /profiles lists the profiles that exist; the "
             "profile project is the item's product, or the "
             "repository named",
        project=project, variant=variant)


def _do_issue_bundle(ident, agent: str, item_id, repo, variant: str):
    conn = store.connect_rw()
    try:
        on_roster = conn.execute(
            "SELECT 1 FROM roster_agents WHERE name = ?", (agent,)).fetchone()
        if on_roster is None:
            raise bundles.BundleRefused(
                422, f"{agent!r} is not on the roster",
                hint="GET /roster lists the agent names this Library knows",
                agent=agent)
        product = None
        if item_id is not None:
            item = conn.execute("SELECT id, product FROM issue_items "
                                "WHERE id = ?", (item_id,)).fetchone()
            if item is None or not identity.in_scope(ident, item["product"]):
                raise bundles.BundleRefused(
                    404, "no such item, or none this identity may see")
            product = item["product"]
            project, scope_kind = product, bundles.SCOPE_ITEM
        else:
            project, scope_kind = repo, bundles.SCOPE_REPO
        # A BOUND IDENTITY MAY ISSUE AGAINST ITS OWN PROFILE ONLY
        # (issue_01M35QG0VANA5P1XE3PH6A7PS4): the agent role carries
        # `bundle.issue`, so without this a bound credential could request a
        # bundle under any OTHER profile and read its documents, which is
        # exactly the binding's point to prevent. Checked before the reload
        # and lookup below, so a mismatch costs no extra work and answers
        # the identical refusal a nonexistent profile gets.
        if _bound_profile_mismatch(ident, project, variant):
            raise _no_such_profile(project, variant)
        # A bundle is issued against the profile the checkout holds now, so the
        # reload runs before this request reads it. It runs after the roster and
        # scope refusals above, so a refused caller cannot make the Library
        # write (the same order as the checkout's reload).
        profile_sync.refresh()
        profile = crew_profiles.profile_row(conn, project, variant)
        if profile is None:
            raise _no_such_profile(project, variant)
        with reference.reading() as index_conn:
            rows = bundles.resolve_bundle(profile, agent, index_conn)
        token_id, expires_at = bundles.mint(
            conn, agent=agent, project=project, variant=variant,
            scope_kind=scope_kind, item_id=item_id, repo=repo,
            product=product, documents=bundles.named_documents(rows),
            issued_by=ident.actor)
        conn.commit()
        scope = {"kind": scope_kind, "project": project, "variant": variant}
        scope["item_id" if item_id is not None else "repo"] = (
            item_id if item_id is not None else repo)
        return 201, {
            "token_id": token_id, "agent": agent, "scope": scope,
            "documents": [reference.doc_dict(r, body=False) for r in rows],
            "count": len(rows), "expires_at": expires_at}
    finally:
        conn.close()


@app.post("/bundles/redeem")
async def redeem_bundle(request: Request):
    """Redeem a bundle token, once, for the documents it names.

    **Body:** `{"token_id": <id from POST /bundles>}`.

    **200:** `{"token_id", "agent", "scope", "documents": [...], "count"}`. Each
    document is `reference.doc_dict`'s shape WITH `body`, so it carries `id`,
    `source_ref`, `kind`, `status`, and `body_sha256`, which is
    `sha256(body.encode("utf-8")).hexdigest()`. An individuator compares the
    hash of a copy it already holds to `body_sha256` to tell that the copy is
    stale. Agent documents also carry `model`, `tools` and `trigger`.

    **Refusals.** 404 for a token that does not exist or is not the caller's to
    see — **including a token minted under a project/variant other than a
    bound identity's own** (issue_01M35QG0VANA5P1XE3PH6A7PS4): the token
    names the documents, so redeeming one issued under another profile would
    hand a bound credential exactly what the binding withholds from every
    other route, the same 404 either way. 409 for a token already redeemed,
    one that has expired, or one whose named documents no longer match the
    Reference Room: each such document is listed with `id`, `reason`
    (`missing`, `not-current` or `changed`) and what the token named. **The
    last case does not consume the token**, because nothing was served; it
    will keep refusing until the Reference Room agrees with the token again,
    or it expires. Ask for a new one.

    **This route carries no identity claim, and that is why it does not run
    `credential_binding.redemption_binding_holds`.** It exchanges a token for
    documents any `reference.read` holder can read, where `POST /tokens/redeem`
    exchanges a call slip for a credential.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "bundle.redeem"):
        return refusals.forbidden("bundle.redeem")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    token_id, err = _bounded_name(payload, "token_id", required=True)
    if err:
        return err
    try:
        status, body = await run_in_threadpool(
            _do_redeem_bundle, ident, token_id)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    except bundles.BundleRefused as e:
        return _bundle_refusal(e)
    return JSONResponse(status_code=status, content=body)


def _do_redeem_bundle(ident, token_id: str):
    conn = store.connect_rw()
    try:
        row = bundles.get(conn, token_id)
        # A BOUND IDENTITY MAY REDEEM ONLY A TOKEN ISSUED UNDER ITS OWN
        # PROFILE (issue_01M35QG0VANA5P1XE3PH6A7PS4): this route carries no
        # identity claim on the token itself — any `bundle.redeem` holder may
        # present any token id — so without this a bound credential handed,
        # or guessing, a token minted under another crew's profile would
        # redeem that crew's documents. The same 404 a nonexistent or
        # out-of-scope token gets, so presenting one leaks nothing about
        # which other profiles a bundle was issued under.
        if (row is None or (row["product"] is not None
                            and not identity.in_scope(ident, row["product"]))
                or _bound_profile_mismatch(ident, row["project"],
                                           row["variant"])):
            return 404, {"error": "no such bundle token"}
        if row["redeemed_at"] is not None:
            return 409, {"error": "this bundle token has already been redeemed",
                         "hint": "a token is redeemable once; ask "
                                 "POST /bundles for a new one"}
        if bundles.is_expired(row):
            return 409, {"error": "this bundle token has expired",
                         "hint": "ask POST /bundles for a new one"}
        with reference.reading() as index_conn:
            rows = bundles.read_named(index_conn, json.loads(row["documents"]))
        if not bundles.consume(conn, token_id, redeemed_by=ident.actor):
            conn.rollback()
            return 409, {"error": "this bundle token has already been redeemed",
                         "hint": "a token is redeemable once; ask "
                                 "POST /bundles for a new one"}
        conn.commit()
        scope = {"kind": row["scope_kind"], "project": row["project"],
                 "variant": row["variant"]}
        if row["scope_kind"] == bundles.SCOPE_ITEM:
            scope["item_id"] = row["item_id"]
        else:
            scope["repo"] = row["repo"]
        return 200, {
            "token_id": token_id, "agent": row["agent"], "scope": scope,
            "documents": [reference.doc_dict(r, body=True) for r in rows],
            "count": len(rows)}
    finally:
        conn.close()


@app.post("/items/{item_id}/release")
async def release_item(request: Request, item_id: str):
    """Hand a hold back: the lease, and the claim when there is one.

    This is the path a session takes on exit. Without it the claim TTL would be
    the only way out of `claimed`, and every clean exit would leave the item
    invisible to the drain for a day.

    **The lease goes back with the claim.** Releasing the claim while keeping the
    fence is the worst of both: the item returns to the queue, a drain picks it
    up, and the checkout is refused by a lease its own previous holder walked away
    from. Scoped to the lease taken FOR this item, so a session holding an epic
    across several items releases one without unfencing the rest.

    **A `pr-open` holder may release too**, and that release drops the FENCE
    without resetting the STATE: the PR is still open, so the item stays `pr-open`
    with its claim intact and does not return to the queue.
    """
    ident, err = _identify(request)
    if err:
        return err
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    owner, err = _claim_owner(payload, ident)
    if err:
        return err

    # **FORCE IS A HUMAN-ONLY ACT AND THIS SERVICE HAS NO HUMANS**, so it is
    # refused rather than ignored. Orient gates it on `kind == "human"`; every
    # token here resolves to `agent` or `system`, so the gate can never pass —
    # and the right behaviour for a capability that cannot be granted is to say
    # so. Silently dropping `force` would let a caller believe it had broken a
    # lease that is still standing.
    #
    # The same gate guards `/checkout`. A forced release breaks a live lease, so
    # leaving this door open would make the checkout path's refusal decorative.
    # A TRUTHY CHECK RATHER THAN `is True` (security-reviewer, PR #6). A caller
    # sending `"force": "true"` or `"force": 1` meant to force, and a strict
    # identity check quietly downgraded that to an ordinary release instead of
    # refusing it. No privilege was gained either way — `force` is never
    # forwarded — but the caller learned nothing about why.
    if payload.get("force"):
        return refusals.refuse(
            403, "only a human may force-release a claim or its lease",
            hint="this service issues no human identity until the Library mints "
                 "its own tokens; release without force returns what you hold")

    try:
        result = await run_in_threadpool(_do_release, ident, item_id, owner)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    status, body = result
    return JSONResponse(status_code=status, content=body)


def _do_release(ident, item_id: str, owner: str):
    """The release itself, on one writable connection so the fence drop and — for
    a claimed item — the claim hand-back move together or not at all.

    A `claimed` release returns the item to `open` and clears the claim. A
    `pr-open` release drops only the fence: the PR is still open, so the work must
    not return to the queue for a drain to pick up and redo.
    """
    conn = store.connect_rw()
    try:
        # **`BEGIN IMMEDIATE` BEFORE THE READ, because this is a check-then-act
        # and `lease.release_claim`'s UPDATE carries no predicate at all**
        # (reviewer, security-reviewer, PR #81) — unlike resolve's own CAS, this
        # write names no prior state to compare against, so the guard has to be
        # the lock rather than the UPDATE. Without it, a concurrent resolve
        # committing between this read and the write below is silently
        # stomped back to `open`, re-queuing work that already finished.
        # `machine_registry.revoke()` is the same shape: the SELECT alone takes
        # no lock, so the lock has to come from here, before it.
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        item = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                            (item_id,)).fetchone()
        if item is None or not identity.in_scope(ident, item["product"]):
            return 404, {"error": "no such item"}
        if not identity.authorized(ident, "item.claim"):
            return 403, {"error": "this identity lacks 'item.claim'"}
        if item["state"] == store.ITEM_STATE_OPEN:
            # **A LEASE-ONLY HOLD IS RELEASED HERE.** A checkout of a blocked
            # item takes the lease without claiming (the operator, 2026-09-12), so its
            # holder has an `open` item and a live lease. Without this branch
            # that lease could only lapse on its grace period, fencing the whole
            # epic until then. `release_for` is scoped to the lease taken for
            # this item by this holder, the same scope the claimed release has,
            # and an open item with no such lease keeps the refusal it had.
            #
            # **THE CREW CHECK RUNS BEFORE `release_for`, on the same scope**
            # (CQ·10): `release_for`'s own guard compares `holder` strings
            # only, so an attacker who read this lease's real holder off it
            # and typed it back as `owner` would otherwise pass straight
            # through.
            existing = lease.live_lease_on(conn, lease.root_id_for(conn, item))
            if (existing is not None and existing["taken_for"] == item_id
                    and _crew_mismatch(ident, _row_crew(existing, "holder_crew"))):
                return 409, _crew_mismatch_refusal()
            released = lease.release_for(conn, item, holder=owner,
                                         actor=ident.actor)
            if released is None:
                return 409, {"error": f"not claimed (state {item['state']})"}
            conn.commit()
            out = store.item_dict(
                item, claim=lease.claim_status(conn, item),
                prerequisites=store.prerequisite_status(
                    conn, item, in_scope=_scope_predicate(ident)))
            out["lease_released"] = lease.lease_dict(released)
            return 200, out
        if item["state"] not in (store.ITEM_STATE_CLAIMED,
                                 store.ITEM_STATE_PR_OPEN):
            return 409, {"error": f"not claimed (state {item['state']})"}
        # **THE CREW CHECK RUNS BEFORE THE `owner` STRING COMPARISON**
        # (CQ·10), for the same reason `_hold_for_write` orders them that
        # way: an attacker who read `claimed_by` off this item and typed it
        # back as `owner` passes the string check below outright.
        if _crew_mismatch(ident, _row_crew(item, "claimed_by_crew")):
            return 409, _crew_mismatch_refusal()
        if item["claimed_by"] != owner:
            return 409, {"error": f"claim held by {item['claimed_by']}"}
        released = lease.release_for(conn, item, holder=owner, actor=ident.actor)
        if item["state"] == store.ITEM_STATE_CLAIMED:
            # A claimed release hands the claim back with the fence: the item
            # returns to `open` and the state event records the transition.
            lease.release_claim(conn, item_id, actor=ident.actor, note=None,
                                prev_owner=owner)
        # A pr-open release deliberately does NOT reach release_claim. The PR is
        # still open, so the item must not return to the queue for a drain to
        # pick up; releasing drops only the fence. `release_for` stamps the lease
        # row released, and no issue_events row is written because no item state
        # transition occurred — a release that leaves the state alone records on
        # the lease, the way a heartbeat does.
        conn.commit()
        fresh = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                             (item_id,)).fetchone()
        out = store.item_dict(
            fresh, claim=lease.claim_status(conn, fresh),
            prerequisites=store.prerequisite_status(
                conn, fresh, in_scope=_scope_predicate(ident)))
        out["lease_released"] = lease.lease_dict(released) if released else None
        return 200, out
    finally:
        conn.close()


@app.post("/items/{item_id}/heartbeat")
async def heartbeat_item(request: Request, item_id: str):
    """Say you are still alive on the lease covering this item.

    **Every lease-bearing action already counts as a sighting**, so a worker that
    keeps touching the queue never needs this. It exists for the holder that goes
    quiet: a session heads-down in code for hours holds a real lease and looks
    dead to a policy that can only see queue writes, and the grace period is
    generous precisely because that shape has no other signal.

    404 when the caller holds no live lease here, rather than taking one. A
    heartbeat that could CREATE a lease would be a checkout by another name, and
    one that skipped the fence.
    """
    ident, err = _identify(request)
    if err:
        return err
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    owner, err = _claim_owner(payload, ident)
    if err:
        return err
    try:
        result = await run_in_threadpool(_do_heartbeat, ident, item_id, owner)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    status, body = result
    return JSONResponse(status_code=status, content=body)


def _do_heartbeat(ident, item_id: str, owner: str):
    conn = store.connect_rw()
    try:
        item = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                            (item_id,)).fetchone()
        if item is None or not identity.in_scope(ident, item["product"]):
            return 404, {"error": "no such item"}
        if not identity.authorized(ident, "item.claim"):
            return 403, {"error": "this identity lacks 'item.claim'"}
        touched = lease.touch(conn, item, owner)
        if touched is None:
            return 404, {"error": f"{owner} holds no live lease covering {item_id}",
                         "hint": "check out the item to take one"}
        conn.commit()
        return 200, {"lease": lease.lease_dict(touched, conn=conn),
                     "policy": lease.policy()}
    finally:
        conn.close()


def _validate_parent(conn, parent_id, *, ident, product, item_id=None,
                     item_type=None):
    """`parent_id` against ADR-0023's nesting rules.

    Returns `(parent_id, refusal)`, where a refusal is a `(status, body)` pair
    rather than a response object, so the re-parent path can use this inside its
    write transaction. Resolving the parent on a read connection and writing on
    another would leave a window where the tree moved between the check and the
    write, which is the one thing a cycle check cannot tolerate.

    **`parent_id` may also name a product's corrections queue**
    (`library/specs/one-home-per-work-item`), a row in `corrections_queues`
    rather than `issue_items` — so the lookup tries that table first and
    falls through to the epic check only when it misses. `item_type` is the
    type the item being parented will have once this request lands (the
    payload's own `type` on create, `effective_type` on `/correct`), and
    `None` when that item will be untyped. A queue accepts any OTHER
    type, since every non-epic item is either an epic's child or a
    correction and a queue IS the home for the second case, but refuses an
    epic exactly as the epic branch below refuses one — an epic's parent is
    an epic or nothing, never a queue.

    Four checks, and **the order of the first two is itself the control**
    (security-reviewer on Orient's PR #154, carried across with the reasoning).
    An out-of-scope parent must be indistinguishable from a missing one: an
    earlier version of Orient's reported the parent's TYPE before checking scope,
    so a caller holding one product's token could probe ids across every product
    and learn which existed, what type they were, and which product they were in.
    Same message, same status, no signal. **A queue in another product gets the
    identical reply**, never the same-product message the epic branch gives,
    so a queue id cannot be used to learn a product name the caller does not
    already have scope over.

    Past that point the caller can already see the parent, so a specific error
    discloses nothing it could not read directly, and a vague one would only make
    a legitimate mistake harder to fix.

    **The fifth check fires only when `item_id` is given**, which is the
    re-parent path. A cycle cannot be closed on create — a new item has no
    descendants to be nested beneath — so create passes no `item_id` and the walk
    is skipped rather than run against nothing. A queue is never an ancestor of
    anything, so no cycle check runs for one.
    """
    if parent_id is None or parent_id == "":
        return None, None
    if not isinstance(parent_id, str) or len(parent_id) > 40:
        return None, (400, {"error": "parent_id must be an item id"})
    queue = store.get_corrections_queue(conn, parent_id)
    if queue is not None:
        if queue["product"] != product or not identity.in_scope(
                ident, queue["product"]):
            return None, (400, {"error": f"parent not found: {parent_id}"})
        if item_type == store.ITEM_TYPE_EPIC:
            return None, (400, {
                "error": "an epic's parent must be an epic or nothing",
                "hint": "a corrections queue holds work items, not epics"})
        return parent_id, None
    parent = store.get_item_for_parenting(conn, parent_id)
    if parent is None or not identity.in_scope(ident, parent["product"]):
        return None, (400, {"error": f"parent not found: {parent_id}"})
    # **THE SAME-PRODUCT CHECK IS UNCONDITIONAL, AND THAT IS THE WHOLE INVARIANT.**
    # It used to be skipped when `product` was None, which made "no edge crosses
    # products" a property of every caller remembering to pass one — and three
    # lease disclosures plus the descendant-count disclosure all rest on that
    # edge. A caller passing None now has every parent refused rather than every
    # check skipped: the escape hatch failed open, and nothing used it (reviewer,
    # PR #12). `tests/test_invariants.py` still reads the call sites, which is now
    # belt-and-braces rather than the only line of defence — an AST test cannot
    # see a None arriving through a variable, and this can.
    if parent["product"] != product:
        return None, (400, {
            "error": f"parent is in product {parent['product']}, not {product}"})
    if parent["type"] != store.ITEM_TYPE_EPIC:
        return None, (400, {
            "error": "parent must be an epic, not a work item",
            "hint": "items do not nest; an item's parent is an epic or nothing"})
    # A CYCLE IS WORSE HERE THAN A NON-TERMINATING ANCESTOR WALK. The lease
    # enumerates a subtree, so a loop hangs the checkout path itself (ADR-0023).
    # Self-parenting is the degenerate case and is caught by the same walk, since
    # an item is its own first ancestor from the destination's side.
    if item_id is not None:
        if parent_id == item_id or item_id in lease.ancestor_ids(conn, parent_id):
            return None, (400, {
                "error": "parent_id would create a cycle",
                "hint": "the destination is inside this item's own subtree"})
    return parent_id, None


def _validate_epic_relation_target(conn, epic_id):
    """`epic_id` against the one rule this relation enforces: an epic that
    exists. Returns `(epic_id, refusal)`, matching `_validate_parent`'s shape.

    **DELIBERATELY NOT PRODUCT-SCOPED, unlike `_validate_parent`.** The
    same-product check there is what the four lease disclosures rest on
    (`_validate_parent`'s own docstring); this relation is the carrier for
    "part of that epic's work" across exactly the boundary that check
    refuses to cross, so scoping this lookup would refuse the one case the
    relation exists for. Nothing here feeds `lease.ancestor_ids`,
    `lease.subtree_ids`, a checkout refusal's fence list, or the epic-retype
    descendant count — all four read `parent_id`, and this writes a separate
    table none of them read.
    """
    if not isinstance(epic_id, str) or not epic_id.strip() or len(epic_id) > 40:
        return None, (400, {"error": "epic_id must be an item id"})
    epic_id = epic_id.strip()
    target = store.get_epic_for_relation(conn, epic_id)
    if target is None or target["type"] != store.ITEM_TYPE_EPIC:
        return None, (400, {
            "error": f"epic_id must name an existing epic, not {epic_id!r}",
            "hint": "the relation names what this item is part of; the "
                    "target must already be filed as an epic"})
    return epic_id, None


@app.post("/items/{item_id}/epic-relations")
async def relate_item_to_epic(request: Request, item_id: str):
    """Write "this item is part of that epic's work" as its own row, so an
    epic in another product can be named without the parent edge
    `_validate_parent` refuses across products
    (issue_01M1JSPH7DBHG0BZFPJPNNZGSC).

    **Gated on `item.resolve`, the same capability `/correct` uses for
    `parent_id`** — writing this relation is the same kind of act on the
    item as setting its parent, so it asks the same permission question.

    **Idempotent**: writing the same `(item_id, epic_id)` pair twice answers
    200 the second time with the row already on file, rather than a
    duplicate or a conflict.

    See `_validate_epic_relation_target` for why the target epic is not
    scoped to the caller's own products, and that function's docstring for
    why this relation reaches none of the four places a `parent_id` edge
    would.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "item.resolve"):
        return refusals.forbidden("item.resolve")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload.get("epic_id"), str) or not payload["epic_id"].strip():
        return refusals.refuse(400, "epic_id is required")
    owner, err = _claim_owner(payload, ident)
    if err:
        return err
    try:
        result = await run_in_threadpool(
            _do_relate_to_epic, ident, item_id, payload["epic_id"], owner)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    status, body = result
    return JSONResponse(status_code=status, content=body)


def _do_relate_to_epic(ident, item_id: str, epic_id: str, owner: str):
    conn = store.connect_rw()
    try:
        item = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                            (item_id,)).fetchone()
        if item is None or not identity.in_scope(ident, item["product"]):
            return 404, {"error": "no such item"}
        if item["state"] in store.ITEM_TERMINAL:
            return 409, {"error": f"item is {item['state']}; a relation "
                                  f"cannot be added to a terminal item"}
        refusal = _hold_for_write(conn, ident, item, owner,
                                  act="relating to an epic")
        if refusal:
            return refusal
        target_epic_id, perr = _validate_epic_relation_target(conn, epic_id)
        if perr:
            return perr
        # `_validate_parent` refuses an item as its own parent; the same
        # holds here (both reviewers, PR #193 round 1).
        if target_epic_id == item_id:
            return 400, {"error": "an item cannot be related to itself"}
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(" ")
        status, out = store.create_epic_relation(
            conn, item_id=item_id, epic_id=target_epic_id,
            created_by=ident.actor, now=now)
        conn.commit()
        return status, out
    finally:
        conn.close()


def _validate_found_during(conn, raw, *, ident, product, item_id=None):
    """`found_during` off a payload: the epic id whose work turned this item
    up, or `None` to clear it. Returns `(found_during_id, refusal)`, a
    `(status, body)` pair in the second slot, matching `_validate_parent`'s
    shape.

    **UNLIKE `_validate_parent`, EVERY REFUSAL PAST A BASIC TYPE CHECK READS
    "unknown item id(s)", THE SAME WORDING `_validate_prerequisites` GIVES AN
    ITEM IT CANNOT RESOLVE.** A non-epic, the item's own id, a missing id, a
    cross-product epic and an epic outside the caller's scope are five
    different facts, and this field answers all five the same way: unlike a
    `parent_id`, a `found_during` id is never a row the request is about to
    attach the item to or beneath, so there is no later disclosure that
    already told the caller which of the five it hit. Giving any of them its
    own wording would make `found_during` an existence oracle one guess at a
    time, exactly what `_validate_prerequisites` already refuses to be for
    `prerequisites.item`.

    `None` or `""` clears it, matching `parent_id`'s convention on this
    route. `item_id` is `None` on create, where self-reference cannot arise
    because the row does not exist yet.
    """
    if raw is None or raw == "":
        return None, None
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 40:
        return None, (400, {"error": "found_during must be an item id"})
    found_during_id = raw.strip()
    refusal = (400, {"error": "unknown item id(s)", "ids": [found_during_id]})
    if found_during_id == item_id:
        return None, refusal
    row = conn.execute(
        "SELECT id, product, type FROM issue_items WHERE id = ?",
        (found_during_id,)).fetchone()
    if row is None or not identity.in_scope(ident, row["product"]):
        return None, refusal
    if row["product"] != product or row["type"] != store.ITEM_TYPE_EPIC:
        return None, refusal
    return found_during_id, None


@app.post("/items")
async def create_item(request: Request):
    """File one Chart House work item.

    **`product` is resolved against the Keel's project registry and the canonical
    slug is stored.** Accepting any short string is how `Substrate` came to sit
    beside `substrate` in the live store — one item, invisible to every view
    asking for `substrate`. Generous about the input and strict about the stored
    value: a slug, an alias, a repo name or any casing of those all resolve, and
    one canonical slug is written.

    Scope is checked against the RESOLVED slug rather than the caller's spelling,
    because a token scoped to `keel` must not be refused for writing `Substrate`,
    and must not be admitted for writing something that merely looks like its
    product.

    **A work item carries no type.** `type` may be omitted, `null`, or `epic`.
    Any other value, the retired `bug`, `nit`, `improvement` and `task` and
    `feature` included, is refused with a 400 naming it.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "item.create"):
        return refusals.forbidden("item.create")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    refused_model = _refuse_model_choice(payload)
    if refused_model:
        return refused_model

    for field in ("product", "title", "type", "severity", "needs", "once_key",
                  "parent_id", "found_during", "body"):
        value = payload.get(field)
        if value is not None and not isinstance(value, str):
            return refusals.refuse(400, f"{field} must be a string when present")
    for field in ("product", "title"):
        if not (payload.get(field) or "").strip():
            return refusals.refuse(400, f"{field} is required")
    if len(payload["title"]) > store.MAX_TITLE:
        return refusals.refuse(
            400, f"title is longer than {store.MAX_TITLE} characters")
    if len(payload["product"]) > store.MAX_IDENT:
        return refusals.refuse(
            400, f"product is longer than {store.MAX_IDENT} characters")

    product = projects.resolve(payload["product"])
    if product is None:
        # The registry was READ and does not know this name — the only case worth
        # refusing on. An unreadable registry returns the input unchanged instead,
        # so a broken registry degrades to accepting anything rather than
        # refusing every filing.
        # NARROWED TO WHAT THIS CALLER MAY FILE INTO (security-reviewer, PR #5).
        # Returning every name in the registry turned a slow oracle into an
        # instant directory dump: a caller could already tell "valid product,
        # wrong scope" from "unknown product" one name at a time, and this handed
        # over the list. It is also the better error — telling a `vivy`-scoped
        # agent that `constance` exists does not help it file correctly.
        known = [n for n in projects.known_names()
                 if identity.in_scope(ident, projects.resolve(n))]
        return refusals.refuse(
            400, f"no project or scope named {payload['product']!r}",
            hint="a slug, alias, repo name or non-project scope resolves; the "
                 "canonical slug is what gets stored",
            known=known)
    if not identity.in_scope(ident, product):
        return refusals.not_found()

    # **A WORK ITEM CARRIES NO TYPE; `epic` IS THE ONLY VALUE LEFT** (the
    # operator, 2026-09-24, issue_01M0EEPS591VQHYEBJY7NGF4C3). No `type` and no
    # default: an item filed with none stores `type = NULL`, same as one
    # filed with `type: null` explicitly.
    #
    # **`bug`, `nit`, `improvement` AND `task` ARE REFUSED like any other
    # unknown value** (issue_01M4J3E6BDF2G6JFGHR1KS9N4J), as `/correct`
    # refuses them. They were accepted and dropped, with `type_ignored` naming
    # the value, while agent-platform's `bin/mcp-ops-server` and Orient's "Log
    # an issue" form still sent one; both now send `type` only for an epic.
    raw_type = payload.get("type")
    if raw_type is None:
        item_type = None
    elif raw_type in store.ITEM_TYPES:
        item_type = raw_type
    else:
        return refusals.refuse(400, f"type {raw_type!r} is not a known type",
                               supported=sorted(store.ITEM_TYPES))
    # REQUIRED, matching Orient (reviewer, PR #5). This was optional in the first
    # version and stored NULL when omitted, which is a silent drop rather than a
    # documented divergence: §4 carries Orient's semantics across, and every other
    # place this route departs from Orient says why in a comment.
    severity = payload.get("severity")
    if severity is None:
        return refusals.refuse(400, "severity is required",
                               supported=sorted(store.ITEM_SEVERITIES))
    if severity not in store.ITEM_SEVERITIES:
        return refusals.refuse(
            400, f"severity {severity!r} is not a known severity",
            supported=sorted(store.ITEM_SEVERITIES))
    needs = payload.get("needs") or store.ITEM_DEFAULT_NEEDS
    if needs not in store.ITEM_NEEDS:
        return refusals.refuse(400, f"needs {needs!r} is not a known value",
                               supported=sorted(store.ITEM_NEEDS))
    # **THROUGH THE SAME VALIDATOR THE CORRECTION PATH USES**, so the two write
    # doors for this field cannot enforce different policies on one downstream
    # consumer. This route previously checked only `isinstance(topics, list)`:
    # no count cap, no length cap, no character restriction, and no DB
    # constraint behind it. Adding a guard on `/correct` alone would have left
    # every bound it enforces reachable by anyone holding `item.create`, which
    # is the ordinary filing permission (security-reviewer, PR #99).
    #
    # Two consequences, both of them this PR's own arguments turned around: a
    # confusable topic would spoof a group at CREATE time with no correction
    # needed, and `sort=topic` runs `json_extract` over every row in the result
    # set — so one item filed with an oversized topics blob would degrade every
    # sorted listing for every caller until something happened to re-validate it.
    new_topics, topics_err = _validate_topics(payload)
    if topics_err:
        return JSONResponse(status_code=topics_err[0], content=topics_err[1])
    topics = new_topics or []
    short_description, sd_err = _validate_short_description(payload)
    if sd_err:
        return JSONResponse(status_code=sd_err[0], content=sd_err[1])
    refs = payload.get("refs") or {}
    if not isinstance(refs, dict):
        return refusals.refuse(400, "refs must be an object")

    conn = store.connect()
    try:
        parent_id, err = _validate_parent(conn, payload.get("parent_id"),
                                          ident=ident, product=product,
                                          item_type=item_type)
        if err:
            return JSONResponse(status_code=err[0], content=err[1])
        # **THE SAME VALIDATOR `/correct` USES, on a fresh item there is
        # nothing yet to self-reference.** `item_id=None` skips that one check
        # rather than needing a special case — see the function's own
        # docstring for why the comparison is already safe with no id at all.
        new_prereqs, prereq_err = _validate_prerequisites(
            conn, ident, payload, item_id=None, old_prerequisites=[])
        if prereq_err:
            return JSONResponse(status_code=prereq_err[0], content=prereq_err[1])
        found_during, fd_err = _validate_found_during(
            conn, payload.get("found_during"), ident=ident, product=product)
        if fd_err:
            return JSONResponse(status_code=fd_err[0], content=fd_err[1])
    finally:
        conn.close()

    row = {
        "id": f"issue_{store.new_ulid()}",
        "product": product,
        "title": store.clip(payload["title"], store.MAX_TITLE),
        "short_description": short_description,
        "body": store.clip(payload.get("body", ""), store.MAX_ITEM_TEXT),
        "refs": json.dumps(refs),
        "topics": json.dumps(topics),
        "prerequisites": json.dumps(new_prereqs or []),
        "severity": severity,
        "state": store.ITEM_STATE_OPEN,
        "type": item_type,
        "created_by": ident.actor,
        "created_by_kind": ident.kind,
        "needs": needs,
        "parent_id": parent_id,
        "found_during": found_during,
        "once_key": payload.get("once_key") or None,
    }
    try:
        status, out = await run_in_threadpool(
            store.insert_item, row, lambda ex: _can_file_into(ident, ex),
            lease.claim_status, _scope_predicate(ident))
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    # ONLY ON 201. A 200 is an idempotent `once_key` hit answering with a row this
    # call did not create, and re-embedding it would be work on somebody else's
    # item; a 409 created nothing at all.
    if status == 201:
        await _embed_after_commit(vectors.VAULT_CHART_HOUSE, out["id"])
    return JSONResponse(status_code=status, content=out)


async def _embed_after_commit(vault: str, item_id: str) -> None:
    """Put a committed row into the vector index, best-effort, holding no lock.

    **AFTER THE COMMIT, AND THAT IS LOAD-BEARING RATHER THAN STYLISTIC.** SQLite
    has one write lock, so a model call awaited inside an open write transaction
    queues every other writer behind it. Orient measured the shape: eight
    concurrent posts, six of them returning 500 at 5.4, 10.8, 16.2, 21.6, 26.9
    and 32.2 seconds — each had waited out `busy_timeout`, so the latency
    tracked the timeout, not the work.

    **BEST-EFFORT, AND THE FAILURE IS LOGGED RATHER THAN RAISED.** A capture is
    usually the only copy, so an embedding failure must never cost the row. What
    it leaves behind is a durable row with `embedded_at` NULL, which
    `vectors.embed_missing` picks up later — the sweep is what makes best-effort
    safe rather than silent, and without it this would be a polite way of dropping
    data.

    **The row's own transaction is already closed when this runs**, so a caller
    reading the response back immediately sees the row whether or not this
    succeeded. `embedded_at` is not in either response shape, so there is nothing
    here for the response to disagree with.
    """
    try:
        await run_in_threadpool(vectors.embed_item_by_id, vault, item_id)
    except Exception as exc:  # noqa: BLE001 — any failure means "not embedded yet"
        print(f"embed failed for {item_id} in {vault}: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)


def _can_file_into(ident, existing: dict) -> bool:
    """Whether a `once_key` collision may be answered with the row it hit.

    The same rule `_can_return_existing` applies to signals, and for the same
    reason: `once_key` is globally unique, so a collision can match an item in a
    product this caller cannot read, and answering with it would make idempotency
    an existence oracle. The reply is a READ of a row this call did not create,
    so the gate asks for the create capability AND the capability every read
    route enforces: `item.create` — the caller could have filed the row — plus
    `item.read`, plus the scope. A role the table grows holding `item.create`
    without `item.read` must not collect a full in-scope item by guessing a key.
    """
    return (identity.authorized(ident, "item.create")
            and identity.authorized(ident, "item.read")
            and identity.in_scope(ident, existing.get("product")))


@app.post("/signals")
async def create_signal(request: Request):
    """Capture one Archives record.

    **The first write route, and the whole design of it is the dedup no-leak
    rule.** `dedup_key` is globally unique, so a collision may match a row in
    another product or a customer-class row the caller could never read. Replying
    with it because the key matched would turn idempotency into an oracle for
    exactly the content the read surface walls off. So the existing row comes back
    only to a caller that could have CREATED it AND could READ it, and every other
    collision gets a bare 409 carrying nothing.

    Carried across from Orient rather than re-derived, since §4 moves these
    semantics as a data migration rather than a redesign: the no-leak gate, the
    field clipping, the identical wording on both denial paths, and the
    `dedup_key` guard on the race catch.

    **The row is embedded after its commit, best-effort**, so a signal captured
    here is reachable through `GET /signals/search`. Both halves of that are
    deliberate and `_embed_after_commit` says why: after, because a model call
    inside an open write transaction holds SQLite's single write lock for its whole
    run; best-effort, because a capture is usually the only copy and must not be
    lost to an embedding failure.

    **An optional `content` string is stored as a blob** (ADR-0020: "records
    point at blobs"), separately from `body`. `body` is what gets embedded
    and searched; `content` is the exact original, for an artifact where
    fidelity to the source matters and a caller wants both — `body` a short
    indexed preview, `content` the full text however long it runs, since
    blobs are not bounded by `MAX_TEXT` the way `body` is. See `blobs.py` and
    `GET /signals/{id}/blob`.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "signal.create"):
        return refusals.forbidden("signal.create")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")

    # TYPE-CHECKED BEFORE USE, because these reach code that assumes a string.
    # A JSON array as `product` reached `identity.in_scope`, where `product in
    # ident.products` raised `TypeError: unhashable type` for any non-wildcard
    # token — a caller-triggered 500 inside the function that enforces product
    # scoping (security-reviewer, PR #4). A non-primitive `dedup_key` reached
    # sqlite3's parameter binding the same way.
    #
    # `privacy_class` was left out of this loop when it was first written, and
    # 500'd two lines below on the same mechanism: `in` against a frozenset hashes
    # the probe (security-reviewer, PR #4 round 2). It is the field that round
    # existed to harden, which is the useful part of the lesson — the guard was
    # written for the fields already on the mind, and the one under active repair
    # was assumed handled. Every caller-supplied scalar goes through here.
    for field in ("product", "dedup_key", "produced_at", "privacy_class"):
        value = payload.get(field)
        if value is not None and not isinstance(value, str):
            return refusals.refuse(400, f"{field} must be a string when present")
    product = payload.get("product")
    if not identity.in_scope(ident, product):
        # 404 RATHER THAN 403, matching every other out-of-scope answer on this
        # service: a caller must not learn which products exist by being told it
        # may not write to one.
        return refusals.not_found()

    # **A BLACK-BOX CREW CONTRIBUTES NOTHING THROUGH THIS PATH** (ADR-0043,
    # issue_01M2M634HK37NWJ5MM13WYYREM clause 7). `register_black_box`'s own
    # module comment says THAT route deliberately does not consult
    # `private_repos()`, because a crew that IS a black box must be able to
    # register. This route runs the opposite direction — a content write, not
    # a registration — so the check belongs here, and the two are not in
    # tension.
    #
    # **FAILS CLOSED, inverting `projects.py`'s usual fail-open trade**,
    # matching `GET /runs`. `private_repos()` returns `None` rather than an
    # empty set when the registry cannot be read, and a caller here must not
    # read that as "nothing is private": the cost of guessing wrong is a
    # black-box crew's content landing in the Archives, which no later edit
    # can retract, while the cost of refusing is a submitter retrying once the
    # registry is readable again.
    #
    # **BEFORE THE DEDUP LOOKUP**, so a black-box product cannot learn about an
    # existing row through a `dedup_key` collision either.
    #
    # **GATED ON `WRITES_ENABLED`, matching the shadow gate `store.insert_signal`
    # enforces at the end of this route.** While the shadow is running nothing is
    # captured regardless of `product` — the request ends in `WritesDisabled`
    # either way — and `store.connect()` requires `DB_PATH` to already exist,
    # which it need not while Orient still owns writes. Asking these two
    # questions in that state would turn a polite "not the write path yet" into
    # an unhandled error over a file that has no reason to exist yet.
    if store.WRITES_ENABLED:
        private = await run_in_threadpool(projects.private_repos)
        crew_names = await run_in_threadpool(projects.private_crew_names)
        if private is None or crew_names is None:
            return refusals.refuse(
                503, "the project registry could not be read, so a "
                     "black-box crew cannot be told apart from an ordinary "
                     "one, and nothing is captured",
                hint="retry once the registry at the configured path is "
                     "readable again")
        names = {product.strip().casefold()} if product else set()
        resolved = projects.resolve(product)
        if resolved:
            names.add(resolved.strip().casefold())
        # CHECKED AGAINST THE UNION of `private_repos()` and
        # `private_crew_names()`, not `private_crew_names()` alone.
        # `private_crew_names()` only counts a project whose `private` value
        # is exactly `True`, while `private_repos()` accepts any truthy
        # value (`"true"`, `1`); dropping `private_repos()` would let a
        # project marked `private: "true"` through. `private_crew_names()`
        # is the one that has to be here at all: `private_repos()` alone
        # keys only a project's slug and its `repos`, so a submission
        # spelled as the project's `display` name slips past it entirely.
        # Live case: the operator's own private project `exampleco` has `display:
        # "exampleco.com"`, a spelling `private_repos()` never sees;
        # `projects.resolve("exampleco.com")` returns `None` because
        # `_resolution_map()` is built from slug/aliases/repos, never
        # `display`; and every live client token is scoped `"*"`, so
        # `product: "exampleco.com"` passed this guard and stored a row
        # before `private_crew_names()` was added here (security-reviewer,
        # PR #172, following PR #160's finding on `reference_admission.py`).
        if names & (private | crew_names):
            # SAME 404 THE SCOPE CHECK JUST RETURNED. Answering a black-box
            # product differently from an out-of-scope one would make this
            # route an oracle for which crews are registered as black boxes.
            return refusals.not_found()

        # A crew that registered ITSELF as a black box is one whether or not
        # the project registry's `private` flag also says so — the two are
        # independent declarations of the same fact.
        registered = await run_in_threadpool(store.black_box_registration_crews)
        if names & registered:
            return refusals.not_found()

    for field in ("source", "kind", "actor"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            return refusals.refuse(400, f"{field} is required")

    # **REFUSED WHEN OVERSIZED, NOT SILENTLY TRUNCATED** (reviewer, PR #4). The
    # first version of this route clipped and returned 201, on the belief that it
    # matched Orient. It did not: Orient's ROUTE refuses these with a 400, and the
    # `_clip` inside its insert core is a last-resort bound for that function's
    # OTHER caller, the CI ingress. So the port had copied the defensive backstop
    # and dropped the actual validation, turning a clean refusal into silent data
    # corruption — on the route ~20 more are about to be modelled on.
    #
    # `clip` still runs in the store, for the same reason Orient keeps it: a
    # second caller must not be able to write past the column's bound.
    for field, cap in (("source", store.MAX_IDENT), ("kind", store.MAX_IDENT),
                       ("product", store.MAX_IDENT), ("actor", store.MAX_ACTOR)):
        if len(payload.get(field) or "") > cap:
            return refusals.refuse(400, f"{field} is longer than {cap} characters")

    # **`body` GETS THE SAME REFUSE-NOT-TRUNCATE TREATMENT, MISSED BY THE LOOP
    # ABOVE** (issue_01M3VN91AP1198SX7HE148ZE1B). `body` is what gets embedded
    # and searched, so a caller whose submission was silently cut at
    # `MAX_TEXT` would have it embedded and read back incomplete with no
    # sign anything was lost. `store.clip` below is still the last-resort
    # bound on this column, same as for the fields above; this is the
    # refusal that keeps it from ever firing on a real request.
    #
    # **NO `or` DEFAULT** (reviewer + security-reviewer, PR #201 round 1).
    # `payload.get("body") or ""` turns every falsy non-string — `0`, `False`,
    # `[]`, `{}` — into `""` before the `isinstance` check below ever sees it,
    # so those four JSON types were still stored as a silent empty capture
    # with a 201: the exact bug this block exists to close. `prose` on the
    # review-record route had the identical `or ""` shape and the identical
    # bug, since fixed the same way. `stream` a few lines below
    # already gets this right — `payload.get("stream")` with no fallback,
    # then an explicit `is not None` — so `body` follows the same shape: only
    # an absent key or a JSON `null` defaults to `""`, and anything else that
    # isn't a `str` is refused rather than coerced.
    body = payload.get("body")
    if body is None:
        body = ""
    elif not isinstance(body, str):
        return refusals.refuse(400, "body must be a string")
    if len(body) > store.MAX_TEXT:
        return refusals.refuse(
            400, f"body is longer than {store.MAX_TEXT} characters")

    # NORMALISED HERE, BEFORE BOTH THE GATE AND STORAGE (reviewer, PR #82). The
    # required-field check above tests only the stripped form, so a padded kind
    # like `"review_verdict "` cleared it, failed the exact-match `in` below,
    # and landed in the column with a kind that reads as a review outcome.
    kind = payload["kind"].strip()

    # **A CHARACTER OUTSIDE PRINTABLE ASCII IS REFUSED, NOT NORMALISED AWAY**
    # (security-reviewer, PR #82 round 3). `.strip()` removes only Unicode
    # whitespace, so a `"review_verdict"` carrying a trailing U+200B zero-width
    # space cleared the presence check, survived the strip, failed the exact
    # `in` below, and stored a kind that reads as `review_verdict` to any
    # terminal, log viewer, dashboard or substring match. Growing what
    # normalisation removes would reopen this for the next character class, so
    # anything outside printable ASCII is refused — the same refused-rather-
    # than-coerced policy `privacy_class` applies a few lines below. The echoed
    # kind is `unicode_escape`-encoded so an invisible character appears as
    # `\u200b` in every log and terminal, never as the raw byte.
    if not all(ch in store.KIND_ALLOWED_CHARS for ch in kind):
        return refusals.refuse(
            400, "kind contains a character outside the printable-ASCII set "
                 "this service accepts",
            hint="an unrecognised character is refused rather than removed or "
                 "replaced, because removing it would store a kind that reads "
                 "as one value and compares as another",
            kind=kind.encode("unicode_escape").decode("ascii"))

    # **REVIEW KINDS ARE GATED ON `signal.create_review`, NOT THE GENERAL
    # `signal.create`** (issue_01M0R0BFE0CFHJM6XR5NG13PF4). A `review_verdict`
    # capture is the row a conditions-based merge gate would read — who approved,
    # at which head — so the writer of that record must not be the subject the
    # gate exists to check. `agent`-role credentials are exactly that subject:
    # every co session and every dispatched agent holds one. The GitHub capture
    # poller, the only current writer of these kinds, is `kind: system`, which is
    # why the capability sits on `system` and not nowhere.
    #
    # A kind that matched the protected set is stored under its CANONICAL
    # lowercase spelling (reviewer, PR #82): the set is closed and lowercase, so
    # folding a member is unambiguous, and it keeps an authorised `Review_Verdict`
    # write from landing in the column as a spelling the exact-match filters and
    # a merge gate would silently miss. Open kinds keep the caller's own casing
    # — folding every kind on the way in would rewrite labels a future producer
    # chose deliberately.
    if kind.lower() in store.SIGNAL_REVIEW_KINDS:
        kind = kind.lower()
    if (kind in store.SIGNAL_REVIEW_KINDS
            and not identity.authorized(ident, "signal.create_review")):
        return refusals.forbidden("signal.create_review")

    # **A `handoff` FOR A PRIVATE PRODUCT IS REFUSED, BEFORE THE DEDUP
    # LOOKUP** (issue_01M383JHPMME74P4HDM46D2WA5, the "Handoffs are memory"
    # epic). A handoff is an ordinary Archive record, read back by `product`
    # like any other, so a private product's handoff must never be stored at
    # all — placed here, ahead of `dedup_key`, for the same no-leak reason
    # the black-box check above runs before the dedup lookup: answering a
    # collision on a private product's handoff would tell an out-of-scope
    # caller that one already exists.
    #
    # **FAILS CLOSED**, matching `projects.private_repos()`'s inverted trade
    # above: `private_products()` returns `None` rather than an empty set
    # when the registry cannot be read, and a handoff is refused rather than
    # captured while the registry cannot say whether its product is private.
    #
    # **Checked against both the raw and the resolved product**, the same
    # shape as the black-box check above, since a wildcard-scoped caller may
    # submit any spelling and the stored row keeps whatever it sent.
    #
    # **`kind` IS MATCHED CASE-FOLDED AND WHITESPACE-TRIMMED** (`_is_handoff`),
    # the same test the SIGNAL_REVIEW_KINDS fold just above applies to its own
    # protected set: `kind` itself keeps the caller's casing for an open kind
    # like `handoff` (only a review kind is folded on the way into storage),
    # so an exact `== "handoff"` here would let `"Handoff"` bypass this gate
    # entirely while reading back identically to every other handoff on every
    # route that applies `_is_handoff` the same way.
    if _is_handoff(kind) and store.WRITES_ENABLED:
        private = await run_in_threadpool(projects.private_products)
        if private is None:
            return refusals.refuse(
                503, "the project registry could not be read, so a private "
                     "product's handoff cannot be told apart from an "
                     "ordinary one, and nothing is captured",
                hint="retry once the registry at the configured path is "
                     "readable again")
        names = {product.strip().casefold()} if product else set()
        resolved = projects.resolve(product)
        if resolved:
            names.add(resolved.strip().casefold())
        if names & private:
            # SAME 404 THE SCOPE AND BLACK-BOX CHECKS ABOVE RETURN. Answering
            # a private product's handoff differently from an out-of-scope
            # product would make this route an oracle for which products are
            # private.
            return refusals.not_found()

    stream = payload.get("stream")
    if stream is not None and (not isinstance(stream, str)
                               or len(stream) > store.MAX_STREAM):
        return refusals.refuse(
            400, f"stream must be a string of at most {store.MAX_STREAM} "
                 f"characters when present")
    # **NO `or` DEFAULT, SAME SHAPE AS `body` ABOVE** (security-reviewer, PR
    # #201 round 2). `payload.get("dedup_key") or None` silently dropped an
    # empty string to `None` rather than storing it or refusing it, and would
    # have let a non-string reach `len()` uncaught here had the type-check
    # loop near the top of this route not already refused it first — this
    # route's own "when present" style refuses both explicitly, so the check
    # here does not depend on that loop running before it. Only an absent key
    # or a JSON `null` means no `dedup_key`; every other value that is not a
    # non-empty string is refused.
    dedup_key = payload.get("dedup_key")
    if dedup_key is not None:
        if not isinstance(dedup_key, str) or not dedup_key:
            return refusals.refuse(
                400, "dedup_key must be a non-empty string when present")
        if len(dedup_key) > store.MAX_DEDUP_KEY:
            return refusals.refuse(
                400, f"dedup_key is longer than {store.MAX_DEDUP_KEY} characters")

    # **NO `or` DEFAULT, SAME BUG AS `body` HAD** (security-reviewer, PR #201
    # round 2). `payload.get("refs") or {}` turned every falsy non-dict —
    # `0`, `False`, `[]`, `""` — into `{}` before the `isinstance` check below
    # ever saw it, so a value like a `proposal_decision`'s `refs: 0` stored as
    # a silent empty `{}` with a 201 rather than being refused. `refs` is not
    # cosmetic: `unreferenced_by` walks every row's `refs` to decide whether a
    # prior row is still live, so a row whose `refs` emptied this way would
    # fail to retire the row it was meant to reference, with no error ever
    # surfacing to the submitter. Only an absent key or a JSON `null` defaults
    # to `{}`; everything else that is not a `dict` is refused.
    refs = payload.get("refs")
    if refs is None:
        refs = {}
    elif not isinstance(refs, dict):
        return refusals.refuse(400, "refs must be an object when present")
    meta = payload.get("meta")
    if meta is None:
        meta = {}
    elif not isinstance(meta, dict):
        return refusals.refuse(400, "meta must be an object when present")

    # **FOR `pr_state`, `product` MUST AGREE WITH WHAT THE REGISTRY DERIVES
    # FROM `meta.repo`, NEVER A GUESS THE CALLER SUPPLIES**
    # (issue_01M2NZG7VT7M80GKSMZ04R5KC7). `GET /signals/pr-state` ranks a
    # PR's rows before it applies scope, correct only while every `pr_state`
    # row for one PR carries the same product — that route's own docstring
    # names this dependency. `_review_record_product` and `_merge_repo_scope`
    # derive a product from a repo the same way (`projects.resolve` on the
    # bare name, the segment after the last `/`); this is the write-time
    # half for the kind whose drift library#124/#125 had to backfill, after
    # a Slack-channel heuristic mis-picked the product for 2,017 rows.
    #
    # **`model_resolution` GETS THE SAME CHECK, BUT ONLY WHEN `meta.repo` IS
    # PRESENT.** agent-platform#603 now sends it on new rows; a row with none
    # (every row written before that change, and a run whose own repository
    # could not be determined) is left exactly as it was, rather than
    # refused for a field its writer never sent.
    #
    # **FAILS CLOSED ON AN UNREADABLE REGISTRY** (503), the same trade the
    # black-box and handoff checks above make: `projects.resolve()` fails
    # OPEN on its own (returns the input unchanged when the registry can't be
    # read), so `projects.available()` is checked first rather than trusting
    # a bare `resolve()` call to notice.
    #
    # **GATED ON `store.WRITES_ENABLED`**, matching the two checks above: while
    # the shadow is running, the write ends in `WritesDisabled` regardless, so
    # there is nothing yet to protect.
    #
    # **A MISMATCH NAMES THE DERIVED PRODUCT ONLY WHEN IT IS NEITHER PRIVATE
    # NOR A BLACK BOX, AND THE CALLER IS IN SCOPE FOR IT.** Every live client
    # token is scoped `"*"`, so the scope test alone filters nothing, and a
    # 400 naming the product would map any private repository to its product
    # for any holder of `signal.create` (security-reviewer, PR #262 round 1).
    # A private or black-box product gets the same 404 the checks above
    # answer with, tested against the same union they use: `private` and
    # `crew_names` are theirs, read under the same `WRITES_ENABLED` gate.
    #
    # **THE ROW STORES THE DERIVED SLUG, NEVER THE CALLER'S SPELLING**, the
    # way `_review_record_product` does (reviewer, PR #262 round 1). A
    # caller may pass a project's alias, which resolves to the same product
    # and so passes the comparison; storing it as spelled would put one PR's
    # rows under two literal products, the drift this check exists to stop.
    # The caller must be in scope for the slug it is stored under.
    if kind in ("pr_state", "model_resolution") and store.WRITES_ENABLED:
        repo = meta.get("repo")
        if kind == "pr_state" or repo is not None:
            if not projects.available():
                return refusals.refuse(
                    503, "the project registry could not be read, so "
                         "meta.repo cannot be resolved to a product, and "
                         "nothing is captured",
                    hint="retry once the registry at the configured path is "
                         "readable again")
            derived = (projects.resolve(repo.strip().rsplit("/", 1)[-1])
                       if isinstance(repo, str) and repo.strip() else None)
            if derived is None:
                return refusals.not_found()
            hidden = (derived.strip().casefold() in (private | crew_names)
                      or derived.strip().casefold() in await run_in_threadpool(
                          store.black_box_registration_crews))
            if hidden or not identity.in_scope(ident, derived):
                return refusals.not_found()
            if projects.resolve(product) != derived:
                return refusals.refuse(
                    400, "product does not match the product the "
                         "registry derives from meta.repo",
                    product=derived)
            product = derived

    # **`meta.blob_ref` IS SERVER-ASSIGNED, NEVER CALLER-SET, and refusing it
    # here is a security boundary rather than a validation nicety.** `GET
    # /signals/{id}/blob` authorizes on the REQUESTING signal's own row —
    # its product and privacy_class — not on whatever row first wrote the
    # blob. If a caller could set `blob_ref` directly, it could point its own
    # (readable) signal at a ref it read off some OTHER, out-of-scope or
    # customer-class signal's `meta` and read that content back through its
    # own row's weaker gate. The only way to attach a blob is to submit its
    # content in THIS call and let the server compute the ref from it.
    if "blob_ref" in meta:
        return refusals.refuse(
            400, "meta.blob_ref is server-assigned from `content`; a caller "
                 "may not set it directly")
    content = payload.get("content")
    if content is not None:
        if not isinstance(content, str):
            return refusals.refuse(400, "content must be a string when present")
        # **A LONE SURROGATE IS VALID JSON AND NOT VALID UTF-8** (reviewer,
        # PR #40). `\ud800` decodes to a Python `str` fine — JSON's `\uXXXX`
        # escape has no obligation to pair a surrogate — and encoding THAT
        # back to bytes is where it stops being a string this route can
        # store, raising `UnicodeEncodeError` uncaught. Refused here in the
        # same 400 shape every other malformed field gets, rather than the
        # generic 500 an unhandled encode error would answer with.
        try:
            content_bytes = content.encode("utf-8")
        except UnicodeEncodeError:
            return refusals.refuse(
                400, "content is not valid UTF-8 (an unpaired surrogate)")
        if len(content_bytes) > store.MAX_BLOB_BYTES:
            return refusals.refuse(
                400, f"content is larger than {store.MAX_BLOB_BYTES} bytes",
                bytes=len(content_bytes))

    # **REFUSED UNLESS IT IS EXACTLY A CLASS WE KNOW**, and this is a real
    # vulnerability rather than tidiness (security-reviewer, PR #4).
    #
    # Every enforcement point for customer content — this gate, the dedup
    # no-leak check, the list filter, and the single-signal wall — compares
    # `privacy_class == "customer"` byte for byte. So `"Customer"` passed this
    # gate with a 201 AND then failed to match any read-side wall, producing a
    # row that was customer content by intent and internal content to every
    # filter. A plain agent could create one and read it straight back.
    #
    # It held until now because Orient was the sole producer and this column was
    # never caller-controlled. This route is the new trust boundary, and an
    # enforcement style that was safe under a trusted producer is not safe here.
    #
    # Refused rather than normalised: coercing `"Customer"` to customer-class
    # would silently accept a typo as a claim about PII, and coercing it the
    # other way is the bug itself. An unknown class is a caller error.
    privacy = payload.get("privacy_class") or "internal"
    if privacy not in store.PRIVACY_CLASSES:
        return refusals.refuse(
            400, "privacy_class is not one of the classes this vault defines",
            hint="an unrecognised class is refused rather than normalised: "
                 "guessing which one was meant would decide whether a row is "
                 "treated as customer content",
            privacy_class=privacy,
            supported=sorted(store.PRIVACY_CLASSES))
    if privacy == store.PRIVACY_CUSTOMER and not identity.authorized(
            ident, "signal.create_customer"):
        # A capability rather than a role check, so the boundary is the same one
        # the read surface enforces on the way out.
        return refusals.forbidden("signal.create_customer")

    # **WHICH PERSON, WHEN A CLIENT CAPTURED THIS FOR ONE**
    # (`issue_01M0K2X44W12A1ACH06FZSWW84`). `ingested_by` records the CREDENTIAL,
    # so a capture Orient makes on a human's behalf reads as Orient having done
    # it and the person is lost. `acting_human` is where the person goes, and it
    # is resolved through `identity.attested_human` — the same one path the
    # acknowledge pair uses, rather than a second attestation rule that could
    # drift from it.
    #
    # **OPTIONAL, AND THAT IS THE DESIGN.** Most captures have no human behind
    # them: the Gmail poller, the CI ingress and every agent filing a signal are
    # machines acting on their own. Requiring a person would break all of them
    # and would invite callers to invent one, which is worse than recording
    # nothing. Absent means absent.
    #
    # **NEVER FALLS BACK TO `ident.actor`**, which `attested_human`'s own
    # docstring names as the thing that must not happen: writing the client into
    # this field records it as having acted, and telling a human's action apart
    # from its client's is the entire point of the column.
    acting_human = None
    if payload.get("acting_human") is not None:
        acting_human, attest_err = identity.attested_human(ident, payload)
        if attest_err:
            return refusals.refuse(403, attest_err["error"])

    # **THE RATE GATE, LAST OF THE REFUSALS AND KEYED ON `ident.actor`**
    # (the operator, 2026-09-30). Every refusal above — auth, scope, a black-box
    # product, a malformed or oversized field — returns before this line, so
    # none of them spends a slot; this is the last thing that can still say no
    # before the first byte is written, which is what "a refused submission
    # writes nothing" requires for THIS refusal too.
    #
    # **`ident.actor` is the identity `_identify` resolved for this request,
    # never a body field.** Keying on anything the caller supplies — `source`,
    # `actor`, `product` — would let one submitter reset its own count by
    # varying a value the Library does not otherwise trust.
    #
    # **AN IDEMPOTENT `dedup_key` RESUBMISSION STILL SPENDS A SLOT.** Whether
    # this call turns out to be a fresh row (201) or a repeat of one already
    # stored (200) is only known inside `store.insert_signal`, below, which
    # runs a real query against the write connection either way — a repeat
    # submission is not free, and checking the dedup table first, before this
    # gate, would need a second read of the same row the insert already reads
    # and would open its own race between the two. The rate this bounds is
    # "how often has this submitter reached the write path", not "how many
    # distinct rows resulted from it".
    allowed, retry_after = store.check_signal_rate_limit(ident.actor)
    if not allowed:
        response = refusals.refuse(
            429, "rate limit exceeded",
            hint=f"at most {store.SIGNAL_RATE_LIMIT} signals per "
                 f"{store.SIGNAL_RATE_WINDOW_SECONDS} s are accepted from one "
                 f"submitter; retry after {retry_after} s",
            limit=store.SIGNAL_RATE_LIMIT,
            window_seconds=store.SIGNAL_RATE_WINDOW_SECONDS,
            retry_after=retry_after)
        response.headers["Retry-After"] = str(retry_after)
        return response

    # **WRITTEN BEFORE THE ROW, AND THAT ORDER IS DELIBERATE.** A blob write is
    # content-addressed and idempotent — writing the same bytes twice costs a
    # `stat` — so doing it first and failing the whole request if it fails
    # leaves nothing half-done. Doing it AFTER a successful row insert would
    # instead risk a row whose `meta.blob_ref` names a blob that was never
    # actually written, which is a worse failure: a reader believes the
    # content exists and gets a 404 finding out it does not.
    if content is not None:
        try:
            ref = await run_in_threadpool(blobs.write_blob, content_bytes)
        except store.WritesDisabled as e:
            return refusals.refuse(503, "the Library is not the write path yet",
                                   hint=str(e))
        except blobs.StorageFull as e:
            return refusals.refuse(
                503, "the Library cannot accept a blob write right now",
                hint=str(e))
        meta = {**meta, "blob_ref": ref, "blob_bytes": len(content_bytes)}

    row = {
        "id": f"sig_{store.new_ulid()}",
        "source": store.clip(payload.get("source"), store.MAX_IDENT),
        "kind": store.clip(kind, store.MAX_IDENT),
        "actor": store.clip(payload.get("actor"), store.MAX_ACTOR),
        "ingested_by": ident.actor,
        # ALREADY BOUNDED AND STRIPPED by `attested_human`, so no `clip` here —
        # a second bound would be a second rule that could drift from the one
        # the acknowledge pair enforces.
        "acting_human": acting_human,
        "product": store.clip(product, store.MAX_IDENT),
        "stream": store.clip(stream, store.MAX_STREAM) or None,
        "body": store.clip(body, store.MAX_TEXT),
        "refs": json.dumps(refs),
        "meta": json.dumps(meta),
        "privacy_class": privacy,
        "dedup_key": dedup_key,
        # NORMALISED OR DROPPED, never stored raw. Orient parses an ISO string to
        # naive UTC and nulls anything unparseable, deliberately rather than
        # failing: a capture must not be lost because a source's timestamp is
        # malformed. Storing the caller's string verbatim would put a
        # non-normalised format into a column both services read from after §4
        # merges the vault (reviewer, PR #4).
        "produced_at": store.parse_timestamp(payload.get("produced_at")),
    }
    try:
        status, out = await run_in_threadpool(
            functools.partial(
                store.insert_signal, row,
                lambda ex: _can_return_existing(ident, ex),
                # SAME GATE AS THE READ SURFACE. A 201 echoes back a value this
                # caller just supplied, so revealing it there discloses nothing
                # — but a 200 is an idempotent `dedup_key` hit answering with a
                # row this call did NOT create, whose attested human may be
                # somebody else's. One capability decides both rather than the
                # status code deciding it.
                reveal_human=identity.authorized(ident, "signal.customer")))
    except store.WritesDisabled as e:
        # 503 RATHER THAN 500: the service is working exactly as configured, and
        # the caller should retry after the cutover rather than treat this as a
        # fault to report.
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    # ONLY ON 201, for the reason `/items` gives: a 200 is an idempotent
    # `dedup_key` hit answering with a row this call did not create.
    if status == 201:
        await _embed_after_commit(vectors.VAULT_ARCHIVES, out["id"])
    return JSONResponse(status_code=status, content=out)


def _can_return_existing(ident, existing: dict) -> bool:
    """Whether a `dedup_key` collision may be answered with the row it hit.

    Only for a caller that could have created it AND could read it, because the
    reply is a READ of a row this call did not create. The create half:
    `signal.create`, plus `signal.create_customer` for a customer-class row,
    plus `signal.create_review` when the row's kind is one whose write is gated
    on it. The read half is the read surface's own gate for the same row
    (`_readable_signal`): `signal.read`, plus `signal.customer` at the customer
    wall. Both halves and the scope, or the idempotent reply reports content the
    read surface would have refused.

    **The capability checks are here because the first version dropped them**
    (reviewer, PR #4). Orient asks `authorize(ident, "signal.create", ex.product)`,
    which is capability AND scope; this port asked only about scope. Nothing was
    exposed today, since both roles that can authenticate hold `signal.create` —
    which is exactly what made it invisible. A role added later with product
    scope and no `signal.create` would have been handed a row it could not have
    written, the same way a role holding `signal.create` without `signal.read`
    would be handed one it could not read. The customer branch asks for
    `signal.customer` as its own read wall because `signal.create_customer` and
    `signal.customer` are only co-held by accident of the role table, and a gate
    may not rely on that.

    **The `signal.create_review` leg is the same shape** (PR #82), added when
    the review kinds became gated: it is invisible today only because `agent`
    also holds plain `signal.read` and the read surface has no kind wall. Both
    legs of the create half and both of the read half are asked, because "could
    have created it" and "could read it" are four questions rather than two.

    **A private product's `handoff` is refused here too** (reviewer finding,
    PR #185 round 1): `dedup_key` is unique across every kind and product, so
    a caller writing an ordinary row could otherwise collide with an existing
    private handoff's `dedup_key` and have `insert_signal` hand it back
    wholesale — a hidden row has to answer exactly as no row would, which
    means this collision path needs the same wall the write gate above places
    ahead of its own dedup lookup, not only that gate's own kind/product.
    """
    if not identity.authorized(ident, "signal.create"):
        return False
    if not identity.in_scope(ident, existing.get("product")):
        return False
    if (existing.get("privacy_class") == store.PRIVACY_CUSTOMER
            and not identity.authorized(ident, "signal.create_customer")):
        return False
    if (existing.get("kind") in store.SIGNAL_REVIEW_KINDS
            and not identity.authorized(ident, "signal.create_review")):
        return False
    if not identity.authorized(ident, "signal.read"):
        return False
    if (existing.get("privacy_class") == store.PRIVACY_CUSTOMER
            and not identity.authorized(ident, "signal.customer")):
        return False
    if (_is_handoff(existing.get("kind"))
            and _private_handoff(existing.get("product"))):
        return False
    return True


@app.get("/signals")
async def list_signals(request: Request):
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "signal.read"):
        return refusals.forbidden("signal.read")
    if (err := _reject_unknown_params(request, {
            "kind", "source", "stream", "product", "repo", "unreferenced_by",
            "since", "limit", "offset", "order", "cursor"})):
        return err
    since_clause, since_params, err = _since_clause(request)
    if err:
        return err
    limit, offset, err = _page(request)
    if err:
        return err
    order_param = request.query_params.get("order")
    if order_param is not None and order_param not in SIGNAL_ORDER_CLAUSES:
        return refusals.refuse(
            400, "order is not a supported value",
            hint="whitelisted rather than interpolated, so an arbitrary column "
                 "name cannot reach the query",
            order=order_param, supported=sorted(SIGNAL_ORDER_CLAUSES))
    order_clause = SIGNAL_ORDER_CLAUSES[order_param or DEFAULT_SIGNAL_ORDER]
    where, params = "WHERE 1 = 1", []
    scope, scope_params = _scope_clause(ident)
    where += scope
    params += scope_params
    # CUSTOMER-AUTHORED CONTENT NEEDS ITS OWN CAPABILITY. A coding agent has no
    # business reading customer PII, and an identity without `signal.customer`
    # must not learn such a row exists — including through a `total` that counts
    # it. See `_customer_wall` for why this is one helper rather than a clause
    # written out at each site.
    wall, wall_params = _customer_wall(ident)
    where += wall
    params += wall_params
    # A `handoff` for a private product must not be listed, counted or
    # paged past, for the same existence-leak reason as the customer wall
    # above — see `_handoff_wall`.
    handoff_wall, handoff_params = _handoff_wall()
    where += handoff_wall
    params += handoff_params
    where, params = _signal_filters(request, where, params, ident)
    where += since_clause
    params += since_params
    # **A CURSOR AND AN OFFSET ARE TWO ANSWERS TO ONE QUESTION**, so taking both
    # would mean silently honouring one. Refusing says which the caller has to
    # choose, and an offset that arrived as the default zero is not a choice.
    cursor = request.query_params.get("cursor")
    if cursor is not None:
        if request.query_params.get("offset") is not None:
            return refusals.refuse(
                400, "pass a cursor or an offset, not both",
                hint="a cursor resumes where a page ended; an offset counts from "
                     "the start. Honouring one and ignoring the other would be a "
                     "different page than the caller asked for")
        clause, cursor_params, err = _cursor_clause(cursor, order_param)
        if err:
            return err
        where += clause
        params += cursor_params

    # **OFF THE EVENT LOOP** (CQ·76), for the reason `signal_facets` gives:
    # the count spans the whole signal set, about 65ms per call on
    # 2026-10-12 and 1.4s on a cold page cache.
    def _read():
        conn = store.connect()
        try:
            # **TWO-TIER SOURCE, ALWAYS** (archives-cold-tier.md §1 row 4) — this
            # route pages and counts across the whole set, so cold must be
            # attached and read unconditionally, not only once a hot miss is
            # found. Attaching here, before `cold` is first referenced, is what
            # the `unreferenced_by` clause `_signal_filters` may have already
            # embedded into `where` depends on.
            reason = store.attach_cold_or_reason(conn, readonly=True)
            if reason:
                return None, None, refusals.refuse(
                    503, f"the archive tier is unavailable: {reason}")
            source = store.signals_source()
            total = conn.execute(
                f"SELECT COUNT(*) FROM {source} AS signals {where}",
                params).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM {source} AS signals {where} ORDER BY {order_clause} "
                f"LIMIT ? OFFSET ?", [*params, limit, 0 if cursor else offset]).fetchall()
        finally:
            conn.close()
        return total, rows, None

    total, rows, refusal = await run_in_threadpool(_read)
    if refusal:
        return refusal
    # `total` counts what REMAINS on a cursor page, because the cursor clause is
    # part of the filter. That is the honest number for a walk — a caller
    # resuming has no use for a count of rows it has already read — and it is why
    # `truncated` compares against it rather than against a running tally the
    # server does not keep.
    # **THE SAME CAPABILITY THE CUSTOMER WALL KEYS ON** (security-reviewer,
    # PR #48). `acting_human` names a person and is written only by an
    # approved-client credential; reading it back has to be gated too, or the
    # write restriction is decorative. `_customer_wall` cannot cover it, because
    # a human may be attested on an `internal`-class capture.
    reveal = identity.authorized(ident, "signal.customer")
    body = {"signals": [store.signal_dict(r, reveal_human=reveal) for r in rows],
            "count": len(rows), "total": total,
            "truncated": len(rows) < total}
    if cursor:
        body["cursor"] = cursor
    else:
        body["offset"] = offset
        body["truncated"] = offset + len(rows) < total
    # The token that resumes this walk, or None at the end of it. Returned on
    # every listing rather than only on a cursor page, so a client can switch to
    # the walk without having to compose a token out of fields it would then have
    # to keep composing correctly.
    #
    # **ONLY FOR AN ORDER THE TOKEN CAN ACTUALLY RESUME.** The token names a
    # position in `ingested_at DESC, id` and says nothing about the order that
    # produced the page, so minting one from a `produced_at` listing hands the
    # client a position that means nothing in the walk it would resume. `None`
    # here is the honest answer for an order this service does not keyset-walk.
    body["next_cursor"] = (_signal_cursor(rows[-1])
                           if rows and body["truncated"]
                           and _cursor_walkable(order_param) else None)
    return body


@app.get("/signals/search")
async def search_signals(request: Request):
    """Semantic search over the Archives.

    **REGISTERED BEFORE `/signals/{signal_id}`, and that ordering is load-bearing**
    — Starlette matches in registration order, so the other way round `search`
    resolves as a signal id and this route is dead code that looks alive.

    **The scope and customer walls are re-applied to the joined rows, and that is
    the whole security property of this endpoint.** Vector search must not become
    a side door around a boundary the list route enforces: a caller that cannot
    list a row must not learn it exists through a similarity score. The KNN is
    oversampled precisely so those filters can remove rows and still fill a page.

    Embedding the caller's QUERY is not a model on the checkout path (ADR-0019).
    That decides what a run receives; this turns a caller's own words into a vector
    so a deterministic index can be searched.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "signal.read"):
        return refusals.forbidden("signal.read")
    if (err := _reject_unknown_params(request, {
            "q", "kind", "limit", "offset", "source", "product"})):
        return err
    q = (request.query_params.get("q") or "").strip()
    if not q:
        return refusals.refuse(400, "q is required",
                               hint="pass the text to search for")
    if len(q) > 2000:
        return refusals.refuse(400, "q is too long", length=len(q))
    if request.query_params.getlist("kind"):
        # **REFUSED RATHER THAN IGNORED, AND RATHER THAN IMPLEMENTED**, which
        # departs from contract §3.2's case 1 — an unrecognised name is dropped
        # silently — so the reason belongs where the code is.
        #
        # Implementing it is the tempting move and is the wrong one. Every filter
        # on this route runs AFTER retrieval, over the oversampled candidate
        # window, so a filter finds only what similarity already pulled in.
        # `kind` is the filter a caller reaches for to enumerate a rare class:
        # 110 `alert` rows in 21,110 signals means a window of `limit * 5`
        # candidates holds well under one of them, so `?kind=alert` would answer
        # an empty page with `truncated: false` — "none in the nearest hundred"
        # reading as "none exist". That is a control whose working state and
        # broken state produce the same evidence, rebuilt as a feature.
        #
        # Ignoring it silently is the same wrong answer with nothing that reports
        # it. So: 400, naming the route that DOES filter by kind, against the
        # whole table rather than a window. Nothing in the fleet sends it today —
        # the agent-facing `signal_search` tool forwards `q`, `limit`, `product`
        # and `source` only — so this breaks no caller, and it turns the next
        # one's wrong answer into an error at its own call site.
        return refusals.refuse(
            400, "kind is not a filter on /signals/search",
            hint="use GET /signals?kind=... — it filters the whole table, "
                 "where this route can only filter what the search retrieved")
    limit, offset, err = _page(request)
    if err:
        return err
    ok, why = vectors.available(store.DB_PATH)
    if not ok:
        # 503 rather than 500: the request is well-formed and this build cannot
        # serve it. A caller that retries elsewhere, or waits, can act on that;
        # a stack trace is not something it can do anything with.
        return refusals.refuse(503, f"semantic search is unavailable: {why}",
                               hint="the deterministic /signals list is unaffected")
    # THE EMBEDDING IS THREADPOOLED; THE QUERY IS NOT. Embedding is the CPU-bound
    # model call and must not block the event loop. The KNN is a fast indexed
    # lookup, and a SQLite connection cannot cross threads — so the connection is
    # opened here, after the model has run, and used only in this one.
    try:
        query_vector = await run_in_threadpool(vectors.embed_query, q)
    except Exception as exc:  # noqa: BLE001 — a model failure is not a 500 the caller can act on
        print(f"query embedding failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return refusals.refuse(
            503, f"the query could not be embedded ({type(exc).__name__})",
            hint="the deterministic /signals list is unaffected")
    conn = vectors.connect(store.DB_PATH)
    try:
        # **THE WINDOW COVERS offset + limit, NOT limit.** An earlier version
        # validated `offset` and then discarded it, so every page was the first
        # one and nothing said so (reviewer, PR #2) — the opposite of this file's
        # own rule that an unusable input is refused rather than quietly accepted.
        # KNN has no cursor, so paging means widening the window and slicing it.
        want = offset + limit
        window = want * vectors.OVERSAMPLE
        cands = vectors.nearest(conn, query_vector, window,
                                vectors.VAULT_ARCHIVES)
        # **SATURATION IS NOT REPORTED, AND THE REASON OVERRIDES THE REASON IT WAS
        # ADDED.** Round 3 added `len(cands) >= window` to `truncated`, to close a
        # real ambiguity: a short page after filtering looks identical to a
        # genuinely short one. Round 4's security review showed that fix leaked.
        #
        # The candidate count is a PRE-FILTER fact — identical for every caller
        # given the same query, whatever their scope or capability. Folding it into
        # a field returned to everyone lets a restricted caller read `count: 0`
        # with `truncated: true` and infer that near matches exist beyond its
        # scope. That is the same inference `list_signals` computes its `total`
        # post-filter to prevent, and the same one ADR-0022 says vector retrieval
        # must not open.
        #
        # So `truncated` is post-filter only, below. The ambiguity round 3 named
        # is real, and `filtered_out` closes the half of it that can be closed
        # without this leak — see the two-stage count after the row fetch. What
        # stays unanswerable is whether the window itself was saturated, and a
        # caller who wants that asks for the next page instead of being told.
        if not cands:
            return {"signals": [], "count": 0, "offset": offset,
                    "truncated": False, "filtered_out": 0, "query": q}
        distance = dict(cands)
        placeholders = ",".join("?" * len(distance))
        # **THE WHERE IS BUILT IN TWO STAGES AND THE SPLIT IS THE SECURITY
        # PROPERTY**, not a tidiness choice. `scoped_where` ends at the caller's
        # own boundary — scope clause and customer wall — and `where` continues
        # into the filters the caller itself passed. `filtered_out` below counts
        # the gap between them, and it is answerable only because the count is
        # taken on this side of the wall. Adding a filter to the wrong one of
        # these two silently changes what the response discloses.
        scoped_where = f"WHERE id IN ({placeholders})"
        scoped_params = list(distance)
        scope, scope_params = _scope_clause(ident)
        scoped_where += scope
        scoped_params += scope_params
        wall, wall_params = _customer_wall(ident)
        scoped_where += wall
        scoped_params += wall_params
        # Part of the same boundary as the scope clause and the customer
        # wall above, not the caller's own filter below — see `_handoff_wall`.
        handoff_wall, handoff_params = _handoff_wall()
        scoped_where += handoff_wall
        scoped_params += handoff_params
        where, params = scoped_where, list(scoped_params)
        # **`source` IS REPEATABLE AND `product` IS NOT, matching `GET /signals`**
        # (`_signal_filters`), so one parameter does not mean two things across
        # the two routes. `source` was a documented parameter of the agent-facing
        # `signal_search` tool, which forwarded it here to a route that never read
        # it: a 200, plausible rows, and a narrowing that never happened
        # (issue_01M0ZXSVZG8K9DGG2CSX0JYE5K).
        #
        # **NOT `_signal_filters` ITSELF, though the drift this route grew is
        # exactly what that helper exists to prevent.** It also serves `kind` and
        # `unreferenced_by`, and `kind` is refused above for a reason that holds
        # only on this route, so delegating wholesale would implement the filter
        # this route deliberately does not have.
        #
        # **THESE RUN AFTER RETRIEVAL, over the oversampled window**, which is the
        # same property the `kind` refusal turns on. `vectors.OVERSAMPLE` is sized
        # for filters of this class, but a source rare enough to miss the window
        # answers zero rather than searching deeper. That is now visible in the
        # response rather than only in the contract: `filtered_out` reports how
        # many of the caller's own visible rows these two clauses removed, so an
        # empty filtered page is legible as inconclusive instead of complete.
        if (sources := request.query_params.getlist("source")):
            where += f" AND source IN ({','.join('?' * len(sources))})"
            params += sources
        if (product := request.query_params.get("product")):
            where += " AND product = ?"
            params.append(product)
        # **ATTACH COLD ONLY IF A CANDIDATE ID IS MISSING FROM HOT**
        # (archives-cold-tier.md §1 row 2) — most KNN candidates resolve in
        # the hot file, which leaves `cold` unattached. A miss here does not
        # distinguish "the id is cold" from "the id is out of scope or
        # walled" — either way the two-tier source is needed to answer
        # correctly, since a scope/wall exclusion must still be excluded
        # after reading from cold too.
        hot_ids = {r[0] for r in conn.execute(
            f"SELECT id FROM signals WHERE id IN ({placeholders})",
            list(distance))}
        source = "signals"
        if len(hot_ids) < len(distance):
            reason = store.attach_cold_or_reason(conn, readonly=True)
            if reason:
                return refusals.refuse(
                    503, f"the archive tier is unavailable: {reason}")
            source = store.signals_source()
        rows = conn.execute(
            f"SELECT * FROM {source} AS signals {where}", params).fetchall()
        # **COUNTED POST-WALL AND PRE-OWN-FILTER, WHICH IS THE ONLY PLACE THIS
        # NUMBER IS BOTH SAFE AND USEFUL** (issue_01M11WR1ZHSDSBZAE1HTZS6P6Z).
        #
        # The defect: `?q=deploy&source=deploy_watch` answered `count: 0,
        # truncated: false` while 11 deploy_watch rows sat in the table, because
        # the window held none of them. The one field a caller reads to tell "no
        # more" from "this is everything" reported completeness on the page
        # missing the most.
        #
        # **The reverted fix is not available and must not be rebuilt.** Round 3
        # of PR #2 folded `len(cands) >= window` into `truncated`; round 4's
        # security review removed it, and the note above `cands` says why — the
        # candidate count is a PRE-FILTER fact, identical for every caller, so
        # returning it lets a restricted caller infer near matches beyond its
        # scope.
        #
        # **What separates the two is WHOSE filter removed the row.** The scope
        # clause and the customer wall are a boundary, and what they removed is
        # exactly what must not be countable. `source` and `product` are the
        # caller's OWN, so what they removed discloses nothing the caller did not
        # already supply. Counting between them reports the second and not the
        # first. A caller reading `count: 0` beside a non-zero `filtered_out`
        # learns its own filter emptied a window that was not empty, and that the
        # answer is inconclusive rather than complete; both at zero stays
        # ambiguous, which is what the wall requires.
        #
        # The extra COUNT runs only when the caller passed a filter of its own —
        # with none, the two stages are the same query and `filtered_out` is 0.
        scoped_count = len(rows)
        if sources or product:
            scoped_count = conn.execute(
                f"SELECT COUNT(*) FROM {source} AS signals {scoped_where}",
                scoped_params).fetchone()[0]
    finally:
        conn.close()
    survivors = sorted(rows, key=lambda r: distance[r["id"]])
    page = survivors[offset:offset + limit]
    # NO `total`, deliberately, and its absence is not the same omission `offset`
    # was. "How many signals in the vault are semantically near this query" has no
    # answer — every vector has some distance — so a `total` here would be the size
    # of an arbitrary window dressed up as a fact. `truncated` is answerable and is
    # what a caller actually needs: more rows survived the filter than this page
    # holds. Window saturation is deliberately NOT part of it — see the note above
    # `cands`, which explains why that pre-filter fact must not reach a caller.
    # **THE SAME CAPABILITY THE CUSTOMER WALL KEYS ON** (security-reviewer,
    # PR #48). `acting_human` names a person and is written only by an
    # approved-client credential; reading it back has to be gated too, or the
    # write restriction is decorative. `_customer_wall` cannot cover it, because
    # a human may be attested on an `internal`-class capture.
    reveal = identity.authorized(ident, "signal.customer")
    return {"signals": [{**store.signal_dict(r, reveal_human=reveal),
                         # Rounded at four places, matching adjacency: full float
                         # precision invites reading the seventh decimal as
                         # meaningful. A DISTANCE, so lower is nearer — named for
                         # what it is rather than converted into a score.
                         "distance": round(distance[r["id"]], 4)} for r in page],
            "count": len(page), "offset": offset,
            "truncated": len(survivors) > offset + limit,
            # **HOW MANY OF THE CALLER'S OWN VISIBLE ROWS ITS OWN `source` /
            # `product` FILTERS REMOVED** — see the two-stage count above. This
            # is what makes an empty filtered page readable: `truncated: false`
            # is true of the page and says nothing about whether the filter had
            # anything to work with, and this says exactly that.
            # **NON-NEGATIVE ONLY BECAUSE SIGNALS ARE APPEND-ONLY** (reviewer,
            # PR #64). The two counts come from two separate statements on a
            # read-only connection, so a row deleted or re-classified between
            # them would make `scoped_count` smaller than the rows already
            # fetched and drive this below zero — a value with no meaning, unlike
            # `list_signals`'s `total`, where the same drift only makes a count
            # stale. `POST /signals` is the one implemented write and it inserts,
            # so nothing can fire it today. The contract's still-unbuilt
            # withdrawing DELETE (§3.3) is what would, and this comment is here
            # so that lands as a change to make rather than as a silent one.
            "filtered_out": scoped_count - len(survivors),
            "query": q}


def _signal_facet_counts(conn, source: str, column: str, where: str,
                          params: list) -> list:
    """`COUNT(*)` grouped by one `signals` column, as `[[value, count], ...]`
    descending by count with the grouped column as the tiebreak — the shape
    `by_source` and `by_kind` on the facets route share, factored so the two
    aggregates cannot drift the way the customer wall's five independent
    `== "customer"` comparisons once did (PR #4).

    **`column` IS A LITERAL FROM THIS FILE, NEVER A REQUEST VALUE** — it
    reaches an f-string, so a caller-supplied column would be an injection
    point, the same rule `_customer_wall`'s `alias` and
    `SIGNAL_ORDER_CLAUSES` document for the same reason. The two callers pass
    `"source"` and `"kind"`; a third column belongs in the response contract,
    not here.

    `source` is the FROM-clause target — `store.signals_source()` aliased
    `AS signals`, with `cold` already attached on `conn`.
    """
    rows = conn.execute(
        f"SELECT {column}, COUNT(*) AS n FROM {source} AS signals {where} "
        f"GROUP BY {column} ORDER BY n DESC, {column}", params).fetchall()
    return [[r[column], r["n"]] for r in rows]


@app.get("/signals/facets")
async def signal_facets(request: Request):
    """Aggregates over the whole set `GET /signals` would page, not a page of
    it — the total, an embedded count, a source breakdown, a kind breakdown,
    and the distinct products present. `signals_build` needs four of them for
    one screen, `release_notes_build` needs `products`, and `by_kind` is how
    a caller learns which kinds the store holds without hard-coding the list;
    each would otherwise page the entire table to collect its answer
    (issue_01KZW183XVJCKFGQT8HMXFJFTY, on 18,455 signals today).

    **REGISTERED BEFORE `/signals/{signal_id}`, and that ordering is load-
    bearing** — Starlette matches in registration order, so the other way round
    `facets` resolves as a signal id and this route is dead code that looks
    alive, the same warning `/signals/search` carries above.

    **The same scope clause, the same customer wall, the same handoff wall,
    and the same `kind` / `source` / `stream` / `product` filters
    `GET /signals` applies, applied here too** — this is the one security
    property of the route. `total` counting a customer-class row a scoped
    caller cannot list is the same disclosure as listing it: the count is not
    a summary of what the caller cannot see, it IS a way to see it. A private
    product's `handoff` is the same disclosure again: it must not inflate
    `total`, appear in `by_kind["handoff"]`, or surface its product in
    `products` (reviewer and security-reviewer, PR #185 round 1 — this route
    applied the scope clause and the customer wall but had no handoff wall of
    its own).

    **`embedded` is a COUNT, not the per-row `embedded_at` stamp** —
    `signal_dict` still omits that field for the reason it always has, that a
    caller cannot act on one row's stamp. Orient renders a count on this
    screen, so a count is what this serves; the reason the field stays off
    every row is untouched.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "signal.read"):
        return refusals.forbidden("signal.read")
    if (err := _reject_unknown_params(request, {
            "kind", "source", "stream", "product", "repo",
            "unreferenced_by", "since"})):
        return err
    since_clause, since_params, err = _since_clause(request)
    if err:
        return err
    where, params = "WHERE 1 = 1", []
    scope, scope_params = _scope_clause(ident)
    where += scope
    params += scope_params
    wall, wall_params = _customer_wall(ident)
    where += wall
    params += wall_params
    handoff_wall, handoff_params = _handoff_wall()
    where += handoff_wall
    params += handoff_params
    where, params = _signal_filters(request, where, params, ident)
    where += since_clause
    params += since_params

    # **OFF THE EVENT LOOP** (CQ·76). These aggregates scan the whole signal
    # set, about 390ms at 92,549 rows on 2026-10-12 and growing with the
    # table, and while they ran on the loop one in-flight call raised the
    # median of twenty concurrent `/health` calls from 28ms to 494ms. The
    # connection opens inside the callable because a SQLite connection cannot
    # cross threads.
    def _build():
        conn = store.connect()
        try:
            # Two-tier source, always (archives-cold-tier.md §1 row 6) — these
            # are aggregates over the whole set `GET /signals` would page.
            reason = store.attach_cold_or_reason(conn, readonly=True)
            if reason:
                return None, refusals.refuse(
                    503, f"the archive tier is unavailable: {reason}")
            source = store.signals_source()
            total = conn.execute(
                f"SELECT COUNT(*) FROM {source} AS signals {where}",
                params).fetchone()[0]
            embedded = conn.execute(
                f"SELECT COUNT(*) FROM {source} AS signals {where} "
                f"AND embedded_at IS NOT NULL", params).fetchone()[0]
            by_source = _signal_facet_counts(conn, source, "source", where, params)
            by_kind = _signal_facet_counts(conn, source, "kind", where, params)
            products = conn.execute(
                f"SELECT DISTINCT product FROM {source} AS signals {where} "
                f"ORDER BY product", params).fetchall()
        finally:
            conn.close()
        return {"total": total, "embedded": embedded,
                "by_source": by_source, "by_kind": by_kind,
                "products": [r["product"] for r in products]}, None

    result, refusal = await run_in_threadpool(_build)
    if refusal:
        return refusal
    return result


@app.get("/signals/acks")
async def signal_acks(request: Request):
    """Which of a set of signals the attested human has acked — the read side
    of the ack pair below. `ops_reports_build` renders acknowledged alerts into
    their own section, and needs to ask this for many signals in one call
    rather than one round trip per row.

    **REGISTERED BEFORE `/signals/{signal_id}`, and that ordering is load-
    bearing** — the same trap `facets` above carries a warning about:
    registered after, `acks` resolves as a signal id and this route is dead
    code that looks alive.

    **Resolved through `identity.attested_human`, exactly as `_ack_request`
    resolves it for the write pair below.** A GET carries no body, so the
    attesting field arrives as the `acting_human` query param instead, and is
    handed to `attested_human` as the same one-key dict a JSON body would have
    carried — one attestation path, not two. The capability check ahead of it
    mirrors `_ack_request`'s own shape: a caller reaches `attested_human` only
    after `signal.ack` has already gated it, the same belt-and-suspenders
    `identity.attested_human`'s docstring describes.

    **`_ack_visible`'s rule applies per id, the same rule `POST`/`DELETE
    /signals/{signal_id}/ack` apply to the one id they take.** An id that is
    out of scope, missing, or customer-class without the capability is simply
    absent from `acked` — confirming it by name, present or absent, would be
    the same disclosure the single-signal routes refuse with a 404 instead of
    a 403.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "signal.ack"):
        return refusals.forbidden("signal.ack")
    if (err := _reject_unknown_params(
            request, {"acting_human", "signal_id"})):
        return err
    payload = {"acting_human": request.query_params.get("acting_human")}
    human, attest_err = identity.attested_human(ident, payload)
    if attest_err:
        return refusals.refuse(403, attest_err["error"])
    signal_ids = request.query_params.getlist("signal_id")
    if len(signal_ids) > MAX_ACK_IDS:
        return refusals.refuse(
            400, f"at most {MAX_ACK_IDS} signal_id values per request",
            hint="refused rather than truncated: a short answer here would report "
                 "the dropped ids as un-acked, which is a wrong answer rather than "
                 "a partial one",
            given=len(signal_ids), max=MAX_ACK_IDS)
    conn = store.connect()
    try:
        visible_ids = [sid for sid in signal_ids if _ack_visible(conn, ident, sid)]
        acked = []
        if visible_ids:
            placeholders = ",".join("?" * len(visible_ids))
            rows = conn.execute(
                f"SELECT signal_id FROM signal_acks WHERE identity = ? "
                f"AND signal_id IN ({placeholders})",
                [human, *visible_ids]).fetchall()
            acked = [r["signal_id"] for r in rows]
    finally:
        conn.close()
    return {"acked": acked}


# The default and maximum for `closed_within_days` on `GET /signals/pr-state`,
# below. Deck has not asked for a specific window, so 14 is a starting choice
# it can move, not a requirement; 90 bounds the parameter the same way every
# other window in this file is bounded, against a caller passing an
# unreasonably large value rather than narrowing its own query.
DEFAULT_PR_STATE_WINDOW_DAYS = 14
MAX_PR_STATE_WINDOW_DAYS = 90


def _repo_run_url_like(repo: str) -> str:
    """The `LIKE` pattern matching a `refs.run_url` whose path names `repo`
    exactly — GitHub's `html_url` for a workflow run is always
    `https://github.com/<owner>/<repo>/actions/runs/<id>`, so a `run_url`
    naming `repo` always carries `f"https://github.com/{repo}/actions/runs/"`
    as a literal prefix, and nothing else does: a different repo sharing
    `repo` as a prefix of its own name (`convivy/vivy` inside
    `convivy/vivy-ownerrez-sandbox-tools`) still fails the match, because the
    character right after `repo` in that case is `-`, not `/`, so the
    required `/actions/runs/` literal never lines up.

    `%` and `_`, `LIKE`'s own wildcards, are escaped first so a repo name
    that happened to contain either matches itself literally rather than as
    a wildcard — no name in `bin/ci_result_fetch.py`'s `TRACKED` does today,
    but nothing about `meta.repo` or `refs.run_url` guarantees that stays
    true.
    """
    escaped = repo.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"https://github.com/{escaped}/actions/runs/%"


def _pr_ci_for_page(conn, rows, scope: str, scope_params: list,
                    wall: str, wall_params: list) -> tuple[dict, set]:
    """The CI conclusion for every row `GET /signals/pr-state` is about to
    return, joined on `refs.commit` against the PR's own `meta.head_sha` —
    one bounded batch for the whole page, not one query per PR.

    `bin/ci_result_fetch.py`'s `signal_from_run` is the only writer: `kind =
    'test-result'`, `source = 'ci'`, `refs.commit` the run's head sha,
    `refs.conclusion`/`refs.workflow`/`refs.run_url` the fields this returns.
    It writes no `repo` field — `product` is the only stored grouping, and
    `TRACKED` in that file maps more than one repo to several products
    (`vivy`, `selector`, `library`, `convivy-orient`, `svc` each cover two).
    `refs.run_url` is the only place a repo survives: GitHub's `html_url`,
    carried verbatim, always has the shape
    `https://github.com/<owner>/<repo>/actions/runs/<id>`.

    **`scope` and `wall` are the same clauses the caller already passed for
    `pr_state` itself, applied here before ranking rather than after:** a
    `test-result` row is walled off from consideration entirely rather than
    ranked and then checked, since (unlike `pr_state`, where the newest row
    for a PR must itself pass scope or the whole PR disappears) a rerun of
    the same workflow against the same commit is always written under that
    commit's own product — there is no case here where the globally-newest
    row for a sha sits in a scope the next-newest row does not.

    **Newest wins by `produced_at DESC NULLS LAST, ingested_at DESC, id
    DESC`, never `SIGNAL_ORDER_CLAUSES`** — the same tie-break
    `pr_state_latest`'s own ranking uses and the same reason: that table's
    `id` half is ascending, kept that way for keyset paging, and would pick
    the OLDEST of two reruns rather than the newest.

    Returns `(ci_by_sha, captured)`: `ci_by_sha` maps a head sha to its
    winning row's `{"conclusion", "workflow", "run_url"}`, only for a sha
    that matched; `captured` is the set of this page's `(product, repo)`
    pairs that have at least one `test-result` row whose `refs.run_url`
    names that exact repo, the fact `pr_state_latest` uses to tell "this
    commit's checks have not finished" from "the Library captures no CI for
    this repo at all" on a sha with no match. **Keyed on `(product, repo)`,
    never on `product` alone** — a captured sibling repo under the same
    product (`convivy/vivy` capturing `vivy` while
    `convivy/vivy-ownerrez-sandbox-tools`, also `vivy`, never has) must not
    make the uncaptured one read as captured (reviewer and security-reviewer,
    PR #226 round 1). A `test-result` row whose `run_url` does not parse as
    `https://github.com/<owner>/<repo>/actions/runs/<id>` — malformed, or
    absent — counts toward no repo at all, matching `ci_captured`'s own
    absence rule: an unparseable row does not make any `(product, repo)`
    pair falsely captured.

    **The sha lookup's `kind = 'test-result'` is the literal string, not a
    bound parameter** — `ix_signals_test_result_commit`
    (`json_extract(refs, '$.commit')) WHERE kind = 'test-result'`) is only
    usable when the query's own `WHERE` clause matches that partial index's
    condition at prepare time, which a `kind = ?` placeholder, unbound until
    execution, cannot prove. `"test-result"` is this function's own choice,
    never a request value, so writing it as a literal costs nothing a bound
    parameter was buying — see the migration's own docstring.

    **`hot_indexed_by="ix_signals_test_result_commit"` forces the index
    rather than leaving the choice to the planner, because this database
    runs no `ANALYZE` on `signals` (round 3 — see the migration's own
    docstring for why one was removed) and without statistics the planner
    does not pick this index on its own: measured on a 16,693-row,
    six-product fixture with no `sqlite_stat1`, the unforced plan is
    `SEARCH signals USING INDEX ix_signals_kind_ingested_at (kind=?)`, the
    whole `test-result` partition, not the new index. `INDEXED BY` also
    means a database this migration has not reached fails the query with
    `sqlite3.OperationalError: no such index`, confirmed against a fixture
    one revision short of this one, rather than silently falling back to
    that same partition scan.

    **The captured check is one `EXISTS` per `(product, repo)` pair on this
    page, not one query over the whole page's products.** Two costs this
    docstring previously claimed do not hold, both measured on the same
    fixture: SQLite drops the `ORDER BY ingested_at DESC` inside an `EXISTS`
    subquery entirely — the `EXPLAIN QUERY PLAN` is identical with or
    without that clause — so a captured repo's most recent run is not
    reached first by design, only by whatever order the chosen index scan
    happens to produce; and without `ANALYZE`, that scan is `SEARCH signals
    USING INDEX ix_signals_kind_ingested_at (kind=?)`, seeking only on
    `kind` and checking `product` as a residual predicate against this
    page's entire `test-result` partition, not the `product`-bound slice
    `ix_signals_product` would give. A captured `(product, repo)` pair can
    still exit early, at whichever row the scan's own order reaches a match
    first; an uncaptured pair has no such exit and reads the whole
    partition before concluding none of its rows name that repo — the same
    bound the old `SELECT DISTINCT product` query paid per product
    regardless of outcome, now paid once per uncaptured pair instead.
    """
    shas = sorted({head_sha for r in rows
                  if (head_sha := store._json_or(r["meta"], {}).get(
                      "head_sha"))})
    if not shas:
        return {}, set()

    sha_source = store.signals_source(
        "kind = 'test-result' AND json_extract(refs, '$.commit') IN "
        f"({','.join('?' * len(shas))})",
        hot_indexed_by="ix_signals_test_result_commit")
    sha_predicate_params = [*shas]
    sha_cte = (
        "WITH ci_ranked AS ("
        "SELECT *, ROW_NUMBER() OVER ("
        "PARTITION BY json_extract(refs, '$.commit') "
        "ORDER BY produced_at DESC NULLS LAST, ingested_at DESC, id DESC"
        ") AS rn "
        f"FROM {sha_source} AS signals WHERE 1 = 1 {scope} {wall}"
        ") SELECT json_extract(refs, '$.commit') AS commit_sha, "
        "json_extract(refs, '$.conclusion') AS conclusion, "
        "json_extract(refs, '$.workflow') AS workflow, "
        "json_extract(refs, '$.run_url') AS run_url "
        "FROM ci_ranked WHERE rn = 1")
    sha_params = [*sha_predicate_params, *sha_predicate_params,
                 *scope_params, *wall_params]
    ci_by_sha = {
        r["commit_sha"]: {"conclusion": r["conclusion"],
                          "workflow": r["workflow"], "run_url": r["run_url"]}
        for r in conn.execute(sha_cte, sha_params).fetchall()}

    repo_pairs = sorted({
        (r["product"], repo) for r in rows
        if r["product"] and
        (repo := store._json_or(r["meta"], {}).get("repo"))})
    captured = set()
    for product, repo in repo_pairs:
        repo_source = store.signals_source("kind = ? AND product = ?")
        repo_predicate_params = ["test-result", product]
        repo_params = [*repo_predicate_params, *repo_predicate_params,
                       *scope_params, *wall_params,
                       _repo_run_url_like(repo)]
        found = conn.execute(
            f"SELECT EXISTS (SELECT 1 FROM {repo_source} AS signals "
            f"WHERE 1 = 1 {scope} {wall} AND "
            "json_extract(refs, '$.run_url') LIKE ? ESCAPE '\\' "
            "ORDER BY ingested_at DESC)", repo_params).fetchone()[0]
        if found:
            captured.add((product, repo))
    return ci_by_sha, captured


@app.get("/signals/pr-state")
async def pr_state_latest(request: Request):
    """The latest state of every pull request, one row per PR, for Deck's PR
    panel — the join `_signal_filters`'s docstring says must live here rather
    than in `LibraryClient`.

    **REGISTERED BEFORE `/signals/{signal_id}`, and that ordering is load-
    bearing** — the same trap `facets`, `acks` and `search` above carry a
    warning about: registered after, `pr-state` resolves as a signal id and
    this route is dead code that looks alive.

    The Bosun's `bin/pr-state-capture` writes a new `pr_state` row whenever a
    PR's head sha, state, draft flag or title changes, so the raw rows repeat
    per PR and this route is what keeps one PR from appearing twice.

    **"Latest" is `produced_at DESC NULLS LAST`, then `ingested_at DESC`,
    then `id DESC`, partitioned by `(meta.repo, meta.number)` — never
    `SIGNAL_ORDER_CLAUSES`.** That table's `id` half is ascending, kept that
    way for keyset paging, and would pick the OLDEST of two tied rows here.
    `produced_at` is null on every row captured before agent-platform#598,
    which is why the order falls through to `ingested_at`, and ids are ULIDs
    so the highest one is the newest.

    **The rank is computed over every `pr_state` row a PR has, not only the
    rows this caller may see, and only the winning row is then checked
    against scope and the customer wall.** Ranking within an already-scoped
    set instead would compute a wrong "latest": if the newest row for a PR
    was captured under a product outside the caller's scope, the caller
    would see the next-newest row it CAN see and read it as current — a
    stale answer wearing the shape of a fresh one. Requiring the
    globally-newest row itself to pass scope and the wall means a PR whose
    true latest state this caller cannot see is dropped from the response
    entirely, rather than shown one state stale. Refusing the PR is the safe
    failure here; a partial one is not.

    **Absence carries information, and this is safe only while two facts
    about `pr_state` rows hold.** Ranking happens before the scope and wall
    checks, so a PR that vanishes tells the caller its newest row is one it
    cannot see (security review, library#129). Two columns decide whether that
    ever happens:

    - **`product`.** On 2026-09-16 every `pr_state` row's product was
      corrected to the registry, and agent-platform#603 now derives it from
      `meta.repo`. So one repository maps to one product, and a PR's rows
      cannot sit in two scopes.
    - **`privacy_class`.** All 5,053 `pr_state` rows on 2026-09-16 were
      `internal`, and `bin/pr-state-capture` never sets the field. So a PR's
      newest row is never walled while an older row is visible.

    **Neither fact is enforced at write time.** `POST /signals` accepts any
    in-scope `product` and either privacy class. If a `pr_state` writer ever
    sets `customer`, or files one PR's rows under two products, this route's
    absences start revealing rows the caller cannot see, and the `product`
    filter below starts returning a stale "latest". Write-time enforcement is
    tracked separately. There is no guard here, because neither case has
    occurred.

    `repo` (repeatable, matching `meta.repo`, the same semantics
    `_signal_filters` gives `GET /signals`) and `product` narrow which PRs
    are considered. Both are the caller's own choice of what to look at, not
    a boundary — unlike scope and the wall, they apply before the rank, over
    the whole table, rather than as a check on the winning row afterward.

    Returned: every PR whose latest state is `OPEN`, plus every PR whose
    latest state is `CLOSED` or `MERGED` and whose latest row falls inside
    `closed_within_days`. The window compares against `produced_at`, falling
    back to `ingested_at` on rows that predate it — the same fallback the
    ordering above uses.

    Ordered by repo, then by PR number descending, so the response is
    reproducible across calls rather than resting on `rowid` order.

    **Each PR also carries its CI conclusion, joined here rather than left
    to Deck** — `_signal_filters`'s own docstring is why this kind of join
    belongs on the server: a client assembling it itself is "a function
    grown inside a client because its view needed one". `ci_conclusion`,
    `ci_workflow` and `ci_run_url` come from the newest `kind = 'test-result',
    source = 'ci'` row whose `refs.commit` equals this PR's `head_sha`,
    scoped and walled the same as this route's own rows (see
    `_pr_ci_for_page`); `ci_captured` is `true` when the Library holds any
    `test-result` row naming this exact `(product, repo)` pair — by
    `refs.run_url`, parsed against `meta.repo`, never by `product` alone,
    since a product can span several repos and a captured sibling must not
    make an uncaptured one read as captured — `false` when it holds none.

    **Three states, two booleans' worth of information, never a missing
    key.** `ci_conclusion` set means a result exists for this exact commit.
    `ci_conclusion` null and `ci_captured` true means this repo's CI is
    captured here but no run has been recorded against this commit yet —
    Deck's "checks have not run". `ci_conclusion` null and `ci_captured`
    false means the Library captures no CI for this repo at all —
    Deck's "unavailable", the state its cockpit already assumes for every
    PR today.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "signal.read"):
        return refusals.forbidden("signal.read")
    if (err := _reject_unknown_params(request, {
            "limit", "offset", "closed_within_days", "repo", "product"})):
        return err
    limit, offset, err = _page(request)
    if err:
        return err
    raw_window = request.query_params.get(
        "closed_within_days", str(DEFAULT_PR_STATE_WINDOW_DAYS))
    try:
        closed_within_days = int(raw_window)
    except ValueError:
        return refusals.refuse(
            400, "closed_within_days must be an integer",
            closed_within_days=raw_window)
    if not 0 <= closed_within_days <= MAX_PR_STATE_WINDOW_DAYS:
        return refusals.refuse(
            400, f"closed_within_days must be between 0 and "
                 f"{MAX_PR_STATE_WINDOW_DAYS}",
            closed_within_days=closed_within_days,
            max_closed_within_days=MAX_PR_STATE_WINDOW_DAYS)
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    since = (now - _dt.timedelta(days=closed_within_days)).isoformat(" ")

    # Caller-chosen narrowing, applied AFTER the two-tier source's own
    # `kind = 'pr_state'` predicate rather than folded into it — never a
    # boundary, so it still runs inside the ranking's own FROM clause rather
    # than after scope and the wall have been applied.
    rank_where, rank_params = "", []
    if (repos := request.query_params.getlist("repo")):
        rank_where += (" AND json_extract(meta, '$.repo') IN "
                       f"({','.join('?' * len(repos))})")
        rank_params += repos
    if (product := request.query_params.get("product")):
        # Correct only while a PR's rows share one product; see the docstring.
        rank_where += " AND product = ?"
        rank_params.append(product)

    scope, scope_params = _scope_clause(ident)
    wall, wall_params = _customer_wall(ident)

    # **TWO-TIER SOURCE, WITH `kind = 'pr_state'` PUSHED INTO BOTH ARMS**
    # (archives-cold-tier.md §1 row 7), so each arm still seeks on
    # `ix_signals_kind_ingested_at` rather than filtering the whole union
    # afterward. The value is bound TWICE below — once per arm — ahead of
    # every other parameter, matching `signals_source`'s own docstring.
    source = store.signals_source("kind = ?")

    cte = (
        "WITH ranked AS ("
        "SELECT *, ROW_NUMBER() OVER ("
        "PARTITION BY json_extract(meta, '$.repo'), "
        "json_extract(meta, '$.number') "
        "ORDER BY produced_at DESC NULLS LAST, ingested_at DESC, id DESC"
        ") AS rn "
        f"FROM {source} AS signals WHERE 1 = 1{rank_where}"
        "), latest AS ("
        "SELECT * FROM ranked "
        f"WHERE rn = 1 {scope} {wall} "
        "AND (json_extract(meta, '$.state') = 'OPEN' "
        "OR (json_extract(meta, '$.state') IN ('CLOSED', 'MERGED') "
        "AND COALESCE(produced_at, ingested_at) >= ?))"
        ") "
    )
    params = ["pr_state", "pr_state", *rank_params, *scope_params,
             *wall_params, since]

    # **OFF THE EVENT LOOP** (CQ·76), for the reason `signal_facets` gives:
    # the window function ranks every `pr_state` row, about 90ms per call on
    # 2026-10-12.
    def _read():
        conn = store.connect()
        try:
            reason = store.attach_cold_or_reason(conn, readonly=True)
            if reason:
                return None, refusals.refuse(
                    503, f"the archive tier is unavailable: {reason}")
            total = conn.execute(
                f"{cte} SELECT COUNT(*) FROM latest", params).fetchone()[0]
            rows = conn.execute(
                f"{cte} SELECT * FROM latest ORDER BY "
                "json_extract(meta, '$.repo') ASC, "
                "json_extract(meta, '$.number') DESC "
                "LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
            ci_by_sha, captured = _pr_ci_for_page(
                conn, rows, scope, scope_params, wall, wall_params)
        finally:
            conn.close()
        return (total, rows, ci_by_sha, captured), None

    read, refusal = await run_in_threadpool(_read)
    if refusal:
        return refusal
    total, rows, ci_by_sha, captured = read
    prs = []
    for r in rows:
        meta = store._json_or(r["meta"], {})
        head_sha = meta.get("head_sha")
        ci = ci_by_sha.get(head_sha) if head_sha else None
        prs.append({
            "repo": meta.get("repo"),
            "number": meta.get("number"),
            "title": meta.get("title"),
            "author": meta.get("author"),
            "draft": meta.get("draft"),
            "head_sha": head_sha,
            "state": meta.get("state"),
            "id": r["id"],
            "produced_at": store._iso(r["produced_at"]),
            "ingested_at": store._iso(r["ingested_at"]),
            "ci_conclusion": ci["conclusion"] if ci else None,
            "ci_workflow": ci["workflow"] if ci else None,
            "ci_run_url": ci["run_url"] if ci else None,
            "ci_captured": (r["product"], meta.get("repo")) in captured,
        })
    return {"prs": prs, "count": len(prs), "total": total, "offset": offset,
            "truncated": offset + len(prs) < total}


def _ci_capture_stats(conn, product: str, repo: str, workflow_file: str,
                      scope: str, scope_params: list, wall: str,
                      wall_params: list) -> tuple[int, str | None]:
    """How many `test-result` signals one `TRACKED` pair, `repo` and
    `workflow_file`, has ever produced, all time, and the newest one's
    `produced_at` (falling back to `ingested_at`) — scoped and walled
    exactly like `_pr_ci_for_page`'s own per-repo `EXISTS` check, which
    this mirrors on the same `kind = ? AND product = ?` plus `refs.run_url
    LIKE` match; see that function's docstring for why the match is on
    `run_url`, not `product` alone.

    **Also matched on `refs.workflow`**, the slug `signal_from_run` writes
    from the run's workflow path, because `TRACKED` holds two pairs for
    `convivy/vivy` (`ci.yml` and `e2e-stage-smoke.yml`). Matched on the
    repo alone, each would report both workflows' rows, and a pair whose
    workflow had never been captured would read as captured."""
    source = store.signals_source("kind = ? AND product = ?")
    predicate_params = ["test-result", product]
    params = [*predicate_params, *predicate_params, *scope_params,
             *wall_params, _repo_run_url_like(repo),
             ci_result_fetch.workflow_slug(workflow_file)]
    row = conn.execute(
        f"SELECT COUNT(*) AS n, "
        "MAX(COALESCE(produced_at, ingested_at)) AS last_at "
        f"FROM {source} AS signals WHERE 1 = 1 {scope} {wall} AND "
        "json_extract(refs, '$.run_url') LIKE ? ESCAPE '\\' AND "
        "json_extract(refs, '$.workflow') = ?", params
    ).fetchone()
    return row["n"], row["last_at"]


@app.get("/signals/ci-capture")
async def ci_capture_report(request: Request):
    """Whether `bin/ci_result_fetch.py` is actually hearing back from
    GitHub for every repository it is supposed to — issue_01M2PERXQA76N5TAYZRFS4AVRN.

    **REGISTERED BEFORE `/signals/{signal_id}`, and that ordering is
    load-bearing** — the same trap `facets`, `acks`, `pr-state` and
    `search` above carry a warning about: registered after, `ci-capture`
    resolves as a signal id and this route is dead code that looks alive.

    **The gap this answers.** A `TRACKED` entry with the wrong
    `workflow_file` gets an empty run list from GitHub with no error, and
    nothing before this route told that apart from a repository whose CI
    is simply quiet. `never_captured` is "tracked, asked, got nothing,
    ever" (zero `test-result` signals exist for this repo and workflow in
    the Library, in any run); `quiet_this_run` is "tracked, asked, got nothing this
    run" on a repo that HAS captured signals before — a different, far
    less worrying state. `TRACKED`, `NOT_CAPTURED` and
    `deck_tracked_repos()` are single-sourced in `bin/ci_result_fetch.py`
    and imported from here, never copied.

    `tracked` carries every `TRACKED` pair's `repo`, `workflow_file`,
    `product`, all-time `test_result_count` and `last_test_result_at`
    (via `_ci_capture_stats`, scoped and walled on the same terms as
    `pr_state_latest`'s own CI join), and `last_fetch` — that pair's own
    entry from `bin/ci_result_fetch.py`'s status file
    (`ci_result_fetch.load_status()`), or `null` when the file is absent,
    unparseable, or simply has no entry for this pair yet.
    `never_captured`, `quiet_this_run` and `unreadable_last_run` are each a
    filtered view of the same `tracked` rows, not a second query.

    `untracked` names every repo `deck_tracked_repos()` yields that
    `TRACKED` does not track and `NOT_CAPTURED` does not explain — the
    same startup check `bin/ci_result_fetch.py`'s own `main()` already
    prints to stderr, read back here for a caller with no access to that
    log. `not_captured` is `NOT_CAPTURED` itself, `{repo, reason}` per
    entry.

    **Every list is filtered to the caller's own products, the same
    `identity.in_scope` test `_scope_clause` compiles to SQL elsewhere** —
    this route has no single `WHERE` to carry it, since `TRACKED`,
    `NOT_CAPTURED` and `deck_tracked_repos()` are Python constants, not
    rows. `tracked` (and so `never_captured`, `quiet_this_run` and
    `unreadable_last_run`, each a view of it) drops a `TRACKED` entry
    whose `product` is out of scope before it is ever queried.
    `untracked` drops a `deck_tracked_repos()` entry the same way, by its
    own `product`. `not_captured` has none of its own — `NOT_CAPTURED`
    maps a repo straight to a reason — so its product comes from
    `deck_tracked_repos()`'s matching entry, and a repo that match misses
    is shown only to a wildcard caller, exactly what `in_scope(ident,
    None)` already returns (reviewer and security-reviewer, PR #229 round
    1 — a product-scoped caller could read every other product's repos,
    workflow files and error text).

    No alert, threshold or failing status here by design
    (issue_01M2PERXQA76N5TAYZRFS4AVRN) — nobody has been bitten by a
    repository going silent AFTER being captured, only by one never
    captured at all, which `never_captured` already shows.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "signal.read"):
        return refusals.forbidden("signal.read")
    if (err := _reject_unknown_params(request, set())):
        return err

    scope, scope_params = _scope_clause(ident)
    wall, wall_params = _customer_wall(ident)

    status = ci_result_fetch.load_status()
    last_fetch_by_key = (status or {}).get("pairs", {})

    # **OFF THE EVENT LOOP** (CQ·76), for the reason `signal_facets` gives:
    # one aggregate per `TRACKED` pair, about 95ms per call on 2026-10-12.
    def _read():
        conn = store.connect()
        try:
            reason = store.attach_cold_or_reason(conn, readonly=True)
            if reason:
                return None, refusals.refuse(
                    503, f"the archive tier is unavailable: {reason}")

            tracked = []
            for entry in ci_result_fetch.TRACKED:
                repo, workflow_file, product = (
                    entry["repo"], entry["workflow_file"], entry["product"])
                if not identity.in_scope(ident, product):
                    continue
                count, last_at = _ci_capture_stats(
                    conn, product, repo, workflow_file, scope, scope_params,
                    wall, wall_params)
                tracked.append({
                    "repo": repo,
                    "workflow_file": workflow_file,
                    "product": product,
                    "test_result_count": count,
                    "last_test_result_at": store._iso(last_at),
                    "last_fetch": last_fetch_by_key.get(
                        f"{repo}#{workflow_file}"),
                })
        finally:
            conn.close()
        return tracked, None

    tracked, refusal = await run_in_threadpool(_read)
    if refusal:
        return refusal

    never_captured = [t for t in tracked if t["test_result_count"] == 0]
    quiet_this_run = [
        t for t in tracked
        if t["test_result_count"] > 0 and t["last_fetch"]
        and t["last_fetch"].get("outcome") == "ok"
        and t["last_fetch"].get("runs_returned") == 0]
    unreadable_last_run = [
        t for t in tracked
        if t["last_fetch"] and t["last_fetch"].get("outcome") == "error"]

    tracked_repos = {e["repo"] for e in ci_result_fetch.TRACKED}
    try:
        deck_repos = ci_result_fetch.deck_tracked_repos()
    except Exception:
        # Matches `bin/ci_result_fetch.py`'s own `main()`: an unreadable or
        # malformed registry costs this one section, never the whole route.
        deck_repos = []
    untracked = [d for d in deck_repos
                 if d["repo"] not in tracked_repos
                 and d["repo"] not in ci_result_fetch.NOT_CAPTURED
                 and identity.in_scope(ident, d["product"])]
    repo_to_product = {d["repo"]: d["product"] for d in deck_repos}
    not_captured = [{"repo": repo, "reason": why}
                    for repo, why in ci_result_fetch.NOT_CAPTURED.items()
                    if identity.in_scope(ident, repo_to_product.get(repo))]

    if status is None:
        status_file = {"state": "absent"}
    else:
        status_file = {"state": "present",
                       "run_finished": status.get("run_finished")}

    return {
        "tracked": tracked,
        "never_captured": never_captured,
        "quiet_this_run": quiet_this_run,
        "unreadable_last_run": unreadable_last_run,
        "untracked": untracked,
        "not_captured": not_captured,
        "status_file": status_file,
    }


# `threshold_bytes_{tier}` query params this route accepts, each landing
# under this key in `storage_report.report`'s `threshold_bytes` mapping —
# one name per tier, kept here rather than inferred from
# `storage_report.TIERS` so a typo'd query param is silently ignored rather
# than raising on a dict comprehension gone wrong.
_STORAGE_THRESHOLD_PARAMS = {
    "threshold_bytes_hot": storage_report.TIER_HOT,
    "threshold_bytes_cold": storage_report.TIER_COLD,
    "threshold_bytes_vectors": storage_report.TIER_VECTORS,
}


# Why `storage.tiers` has no per-product share to fall back to — shared by
# every reason text `_storage_report_wide` can return below.
_WIDE_REASON_PREFIX = (
    "size and growth are physical facts about library.db, cold.db and "
    "vectors.db, each one file shared by every product, with no "
    "per-product share to report instead of the platform-wide total; ")

# The three gates `_storage_report_wide` checks, in the order it checks
# them — each reason names the one gate that actually stopped THIS caller,
# not the other two it may also fail, so a caller cannot read its own
# reason as describing a condition it already satisfies (reviewer, PR #203
# round 3: the single reason this used to return named only the first gate,
# which misled a caller withheld by the second or third).
_WIDE_REASON_NOT_WILDCARD = _WIDE_REASON_PREFIX + (
    "this identity is not scoped to every product (\"*\")")
_WIDE_REASON_NO_CUSTOMER = _WIDE_REASON_PREFIX + (
    "this identity is scoped to every product (\"*\") but lacks "
    "signal.customer, so it cannot read a customer-class row through "
    "_customer_wall even though the platform-wide figures are computed "
    "over one regardless of product")
_WIDE_REASON_HANDOFF = _WIDE_REASON_PREFIX + (
    "a handoff-walled row for a private product exists right now, and "
    "_handoff_wall excludes it for every caller including this one, so "
    "the platform-wide figures — computed over that row too — are "
    "withheld until no such row exists")


def _storage_report_wide(ident: identity.Identity, conn,
                         source: str) -> str | None:
    """`None` if `ident` may receive `storage_report.report`'s platform-wide
    block — `tiers`, `measurement.current_project_count`, and the vectors-
    growth ratio, which `storage_report.py`'s docstring explains is computed
    unscoped by product, customer class AND the handoff wall, deliberately
    — else the reason naming which of the three gates below withheld it.
    Three conditions, not `"*" in ident.products` alone (security-reviewer,
    PR #203 round 2): a `"*"`-scoped caller still cannot read a customer-
    class row without `signal.customer`, through `_customer_wall`, and NO
    identity is exempt from `_handoff_wall` — a `kind = 'handoff'` row for a
    private product is excluded for every caller alike, not scoped per
    identity (see `_handoff_wall`). So a `"*"`-scoped, `signal.customer`-
    holding caller still cannot read such a row through any route this
    service offers, and the platform-wide figures must be withheld from it
    too whenever one exists in `source` — checked here by running
    `_handoff_wall`'s own predicate as a query, the same SQL functions
    `_register_handoff_sql_functions` registers, rather than asking the
    project registry alone: a private product with no handoff signal today
    restricts nobody in practice, and withholding the figures from
    everybody over a product that merely could someday host one would be a
    scoped variant of `wide` reached by a different route than the ratio's
    own math — the one this PR's fix explicitly keeps out of
    `storage_report.report`.

    **Naming which gate stopped this caller costs nothing extra** — the
    three checks below already run in this order and return as soon as one
    fails, so the gate that stopped a given caller is exactly the one
    `return` reached; there is no separate measurement to make to learn it.

    **The handoff check below is deliberately NOT cached, unlike
    `storage_report.tier_sizes`/`_embedded_bytes_total`** (reviewer +
    security-reviewer, PR #203 round 3): this is an admission decision, not
    a measurement, and a cached "no walled row" answer would keep admitting
    a caller for up to the cache window after a walled row appears,
    reopening exactly the gap this PR's own third condition exists to
    close. It runs fresh, on every request, for every caller this far.

    **`SELECT EXISTS (...)` rather than a bare `SELECT 1 ... LIMIT 1`** —
    the same early exit once a matching row is found (SQLite applies its
    own LIMIT-1 optimization inside an `EXISTS` subquery), spelled as what
    the query actually is: an existence check, not a fetch. Neither form
    can seek an index here: `kind` has no index at all
    (`alembic/versions/0b1e7c4a9d52_baseline_the_migrated_vaults.py`
    indexes `ingested_at`, `stream`, `embedded_at`, `privacy_class`,
    `source` and `product` on `signals`, not `kind`), and wrapping `kind`
    and `product` in the registered scalar functions above forecloses
    pushdown through any index either column did have — SQLite must call
    the function per row to know which rows match, the same reason a
    function-wrapped predicate never seeks in `storage_report.py`'s own
    `tier_sizes`/`_embedded_bytes_total`. So the expected case — no
    handoff-walled row anywhere — is a full scan of `source`
    (`EXPLAIN QUERY PLAN`: `SCAN main.signals`, `SCAN c`), confirmed on a
    fixture: 500,000 non-matching rows in `main.signals` alone, no handoff
    row anywhere, cost ~58ms per call (`SELECT 1 ... LIMIT 1` and `SELECT
    EXISTS (...)` measured within noise of each other, both a full scan).
    Adding an index on `kind` would need a migration, which is a schema
    change this PR does not make — raised in the PR body instead of
    slipped in here. Pre-filtering on a literal `product IN (...)` list
    computed from the registry (cheap, and `product` does have an index)
    was considered and rejected: it would reintroduce exactly the
    hand-rolled-approximation drift risk `_private_handoff`'s own
    docstring and PR #185 round 3 fixed — `product` has to be resolved
    through `projects.resolve()` (aliases, repo-name spellings, whitespace)
    before it can be compared to the private-products list, which only the
    registered SQL function does correctly today.
    """
    if "*" not in ident.products:
        return _WIDE_REASON_NOT_WILDCARD
    if not identity.authorized(ident, "signal.customer"):
        return _WIDE_REASON_NO_CUSTOMER
    row = conn.execute(
        f"SELECT EXISTS (SELECT 1 FROM {source} AS signals WHERE "
        f"is_handoff_kind(signals.kind) AND "
        f"is_private_handoff_product(signals.product))").fetchone()
    return _WIDE_REASON_HANDOFF if row[0] else None


@app.get("/signals/storage")
async def signal_storage(request: Request):
    """Per-tier size and growth, and `library_vectors`'s composition per
    kind, projected for twenty concurrent projects over five years — clauses
    10-11 of the memory-retention epic (issue_01M2F52JSSTEW7EM9TMB342J15),
    serving issue_01M35R5XRDJBWZFJ87NTGJVTF5. The arithmetic itself lives in
    `storage_report.py`; read that module's docstring first; it explains why
    there is no built-in byte threshold.

    **REGISTERED BEFORE `/signals/{signal_id}`, and that ordering is load-
    bearing** — the same trap `facets`, `acks`, `pr-state` and `search`
    above carry a warning about: registered after, `storage` resolves as a
    signal id and this route is dead code that looks alive.

    **Tier sizes reach only a caller who can read every row they are
    computed over** — `"*"` in its products, `signal.customer`, and no
    product the handoff wall is currently excluding a row for (see
    `_storage_report_wide`). `tiers` is physical facts about this service's
    own database files — `library.db`, `cold.db` and `vectors.db` are each
    one shared file, with no per-product share to report instead of the
    platform-wide total — but the vectors-growth ratio folded into `tiers`
    is computed from customer-class and handoff-walled content too
    (`storage_report.py`'s docstring), so `"*"` products alone is not
    enough: a caller failing any of the three gets `tiers` and
    `measurement.current_project_count` withheld rather than a figure that
    does not exist, or one derived from rows it cannot read anywhere else
    (security-reviewer, PR #203 rounds 1 and 2: round 1, an unscoped total
    told a single-product token the combined byte totals and project count
    of every other product, including customer-class and private-crew data
    `GET /metrics/librarian` already gates the same way; round 2, a
    `"*"`-scoped caller without `signal.customer`, or walled off from a
    handoff, would otherwise receive the platform-wide ratio anyway).
    `storage_report.report`'s `wide` paragraph has the detail. Each withheld
    tier's `"reason"` (and the top-level `"note"`) names the ONE gate that
    actually stopped this caller, not a generic restatement of all three
    (reviewer, PR #203 round 4) — `_storage_report_wide` returns that text
    directly, since it already knows which check it failed.

    **The ingestion-derived growth rate and the per-kind share of
    `library_vectors` ARE scoped**, by the same scope clause, customer wall
    and handoff wall `GET /signals/facets` applies to `total` and `by_kind`
    — a kind breakdown is the same per-signal disclosure facets' own
    docstring names.

    **The dbstat measurement this route's `tiers` block needs runs off the
    event loop, and only for a caller `_storage_report_wide` admits** — the
    majority of callers, scoped to one product, pay for none of it. See
    `storage_report.tier_sizes` for the equality-constrained query and its
    short-lived cache, both added for the same reason (reviewer +
    security-reviewer, PR #203 round 1): a `LIKE`-filtered `dbstat` walks
    every page of the whole schema, `main` included, which also holds the
    entire Chart House. The ratio's own unscoped denominator,
    `storage_report._embedded_bytes_total`, shares that cache window too
    (reviewer, PR #203 round 2 non-blocking), keyed per `source` since
    round 3 (security-reviewer suggestion). The handoff-existence check
    that `_storage_report_wide` itself runs is NOT cached, deliberately,
    and is a full unindexed scan in the common case — see its own
    docstring for why, and for the measured cost.

    `window_days` (default `storage_report.DEFAULT_GROWTH_WINDOW_DAYS`,
    bounded like every other window in this file) sets how far back the
    ingestion measurement looks. `threshold_bytes_hot`, `threshold_bytes_cold`
    and `threshold_bytes_vectors` are each optional: pass one to get that
    tier's `threshold_date`, a crossing date computed against a number this
    route does not invent — see the module docstring for why none is
    built in.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "signal.read"):
        return refusals.forbidden("signal.read")
    if (err := _reject_unknown_params(request, {
            "window_days", *_STORAGE_THRESHOLD_PARAMS})):
        return err
    raw_window = request.query_params.get(
        "window_days", str(storage_report.DEFAULT_GROWTH_WINDOW_DAYS))
    try:
        window_days = int(raw_window)
    except ValueError:
        return refusals.refuse(400, "window_days must be an integer",
                               window_days=raw_window)
    if not 1 <= window_days <= 3650:
        return refusals.refuse(400, "window_days must be between 1 and 3650")
    threshold_bytes = {}
    for param, tier in _STORAGE_THRESHOLD_PARAMS.items():
        raw = request.query_params.get(param)
        if raw is None:
            continue
        try:
            value = int(raw)
        except ValueError:
            return refusals.refuse(400, f"{param} must be an integer",
                                   **{param: raw})
        if value < 0:
            return refusals.refuse(400, f"{param} must not be negative",
                                   **{param: value})
        threshold_bytes[tier] = value

    where, params = "WHERE 1 = 1", []
    scope, scope_params = _scope_clause(ident)
    where += scope
    params += scope_params
    wall, wall_params = _customer_wall(ident)
    where += wall
    params += wall_params
    handoff_wall, handoff_params = _handoff_wall()
    where += handoff_wall
    params += handoff_params

    def _build():
        conn = vectors.connect(store.DB_PATH)
        try:
            reason = store.attach_cold_or_reason(conn, readonly=True)
            if reason:
                return None, refusals.refuse(
                    503, f"the archive tier is unavailable: {reason}")
            source = store.signals_source()
            wide_reason = _storage_report_wide(ident, conn, source)
            result = storage_report.report(
                conn, source=source, where=where, params=params,
                current_project_count=len(projects.projects()),
                window_days=window_days, threshold_bytes=threshold_bytes,
                wide=wide_reason is None, withheld_reason=wide_reason)
            return result, None
        finally:
            conn.close()

    result, refusal = await run_in_threadpool(_build)
    if refusal:
        return refusal
    return result


def _readable_signal(ident, signal_id: str):
    """One signal row `ident` may read, or a refusal. Shared by `GET
    /signals/{id}` and `GET /signals/{id}/blob` so the two authorization
    checks — product scope, then the customer-class wall — cannot drift
    apart under a future edit to one and not the other (reviewer, PR #40).
    """
    if not identity.authorized(ident, "signal.read"):
        return None, refusals.forbidden("signal.read")
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT * FROM signals WHERE id = ?", [signal_id]).fetchone()
        # **HOT FIRST, THEN COLD ON A MISS** (archives-cold-tier.md §1 row
        # 1) — most ids resolve in the hot file, which leaves the cold file
        # unattached, as the build plan's "attached on demand" says. A miss
        # that cannot attach cold answers 503, never 404 — a genuine 404
        # only comes from a miss in BOTH tiers.
        if row is None:
            reason = store.attach_cold_or_reason(conn, readonly=True)
            if reason:
                return None, refusals.refuse(
                    503, f"the archive tier is unavailable: {reason}")
            row = conn.execute(
                "SELECT * FROM cold.signals WHERE id = ?",
                [signal_id]).fetchone()
    finally:
        conn.close()
    if row is None or not identity.in_scope(ident, row["product"]):
        return None, refusals.not_found()
    if (row["privacy_class"] == store.PRIVACY_CUSTOMER
            and not identity.authorized(ident, "signal.customer")):
        # 404 rather than 403, matching the list filter above: a 403 here would
        # confirm the row exists to an identity that may not know it does.
        return None, refusals.not_found()
    if _is_handoff(row["kind"]) and _private_handoff(row["product"]):
        return None, refusals.not_found()
    return row, None


@app.get("/signals/{signal_id}")
async def get_signal(signal_id: str, request: Request):
    ident, err = _identify(request)
    if err:
        return err
    if (err := _reject_unknown_params(request, set())):
        return err
    row, err = _readable_signal(ident, signal_id)
    if err:
        return err
    return store.signal_dict(
        row, reveal_human=identity.authorized(ident, "signal.customer"))


@app.get("/signals/{signal_id}/blob")
async def get_signal_blob(signal_id: str, request: Request):
    """The exact original content a signal's `meta.blob_ref` points at.

    **Authorized on THIS row's own product and privacy_class, never on
    whatever row first wrote the blob.** That is what makes refusing a
    caller-set `blob_ref` in `create_signal` a security boundary rather than
    a nicety: this route trusts the row it read, and the row can only ever
    carry a ref the server itself computed from content that row's own
    creator actually submitted, in the request that created it.
    """
    ident, err = _identify(request)
    if err:
        return err
    if (err := _reject_unknown_params(request, set())):
        return err
    row, err = _readable_signal(ident, signal_id)
    if err:
        return err
    ref = store._json_or(row["meta"], {}).get("blob_ref")
    if not isinstance(ref, str):
        # The row is real and readable — confirmed above — so saying it has
        # no blob discloses nothing `GET /signals/{id}` did not already, via
        # the same `meta` field in plain view there.
        return refusals.refuse(404, "this signal carries no blob")
    try:
        content = await run_in_threadpool(blobs.read_blob, ref)
    except blobs.InvalidRef:
        # A malformed ref in a row's own meta is a data-integrity problem,
        # not a caller error.
        return refusals.refuse(500, "this signal's blob_ref is malformed")
    except FileNotFoundError:
        return refusals.refuse(
            404, "this signal's blob is missing from storage",
            hint="the row exists but its blob was never written, or was lost")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return refusals.refuse(
            500, "this signal's blob is not valid UTF-8",
            hint="every blob is written from a JSON string, so this means "
                 "the stored bytes were corrupted or tampered with")
    return PlainTextResponse(text)


# --- the Reference Room -------------------------------------------------------
#
# NOT PRODUCT-SCOPED, and `reference.py` carries the argument in full. In short:
# the Chart House and the Archives are scoped hard because a caller must not learn
# another product's work exists, while reviewed documents are company knowledge
# that the dispatch machinery deliberately shares across repos — scoping them the
# way items are scoped would take ADR-0019 through ADR-0022 away from every agent
# that needs them. `project` remains available as a filter, which is a caller
# narrowing its own results rather than a boundary it cannot cross.


# --- why there is no audience wall here -----------------------------------------
#
# **THERE WAS ONE, AND THE OPERATOR DENIED IT ON 2026-08-08** (`dec_01KZH8XTJWXK5SV2NJBCD0J3C1`).
# The record stays because the exposure is real and the next person to read these
# routes should find the reasoning rather than re-derive it — or, worse, assume
# nobody looked.
#
# WHAT IS EXPOSED. Every `reference.read`-capable token can read `company/legal/legal`
# and `company/legal/entity-operations` — Convivy LLC's entity structure, the
# founders' community-property status, personal tax and banking detail — plus
# `company/reference/people`, which exists in a version deliberately trimmed of
# exactly that material for the contractor tier. An agent processing
# attacker-influenced content can be steered into reading one and echoing it into a
# PR body or a log.
#
# WHY IT IS OPEN ANYWAY. Convivy is one operator and two people, every agent runs on
# the operator's own machines against the operator's own data, and walling 42% of
# the KB — 260 of 615 live documents carry a human-only audience — costs more today
# than the exposure does. No decision is needed to proceed with any phase.
#
# **WHAT CHANGES THAT: go-to-market, or any public or multi-tenant access.** The
# moment a token exists that is not the operator's, this stops being a cost/benefit call and
# becomes a disclosure. Tracked as `issue_01KZHD2AHS6MB4ZGYQFGPZPD2R`, parked with
# that as its gate rather than closed, so it surfaces when the condition arrives
# instead of being remembered.

def _status_clause(request: Request):
    """The `status` filter for the two Reference Room read routes, as
    `(clause, params, refusal)` — one helper so the pair cannot disagree about
    which corpus they answer for (issue_01M1MWRXAZ5NRH10NP6Z9MBH0C).

    **AN UNRECOGNISED STATUS IS A 400, NOT AN EMPTY PAGE.** `?status=all` used
    to answer `200` with `total: 0`, which is indistinguishable from a corpus
    that genuinely holds nothing under that filter — a broken call and a
    working one producing identical evidence. `GET /signals/search` already
    refuses its `kind` for this reason and carries `filtered_out` "so an
    emptied page is legible as inconclusive"; this is that rule one route over.

    **IT IS A SECOND REASON A FILTER MAY VALIDATE, NOT AN EXTENSION OF THE
    FIRST.** §3.2's case 3 gives whitelisting as its reason — keeping an
    arbitrary column name out of the query — and cautions against reading case
    3 as the norm a filter grows into. `status` needs no whitelisting: it is a
    bound parameter and always was. The reason here is legibility, which is a
    narrower door and stays narrow: it is open only where the vocabulary is
    CLOSED, so a refusal can name the whole of it, and where that vocabulary is
    NON-SENSITIVE. The caution survives intact because it is about
    whitelisting, and this is not that.

    **THE NAMED SET CARRIES NOTHING PRIVILEGED, WHICH THE CONTRACT MAKES A
    CONDITION RATHER THAN A NICETY.** §3.2: "A validating 400 is safe only
    while the permitted set it names is itself non-sensitive." `status`'s
    vocabulary is fixed in `reference.ALLOWED_STATUSES`, identical for every
    caller, declared in the contract, and derived from no data — it carries no
    scope or existence information, so a refusal naming it hands back nothing
    a caller could not read here. A filter validating against a set derived
    from data would rebuild the enumeration oracle §8's single-404 wording
    exists to deny; this one cannot, because there is no data in it.
    """
    # **EMPTY IS ABSENT, NOT A BAD VALUE.** `?status=` is how a client
    # serialises an optional field it is not setting, and the truthiness check
    # this replaced already treated it as "no filter". Refusing it would turn a
    # request that works today into a 400 — a tightening, which is a version-2
    # move (contract §1), reached by accident through an `is None` that reads
    # more precise than it is.
    status = request.query_params.get("status") or None
    if status is None:
        placeholders = ",".join("?" * len(reference.DEFAULT_STATUSES))
        return (f"d.status IN ({placeholders})",
                list(reference.DEFAULT_STATUSES), None)
    if status == reference.STATUS_ALL:
        # NO CLAUSE AT ALL rather than an IN over every status. The two are
        # equivalent today and stop being equivalent the moment a status is
        # added: an IN built from this service's copy of the vocabulary would
        # silently exclude a document the index already holds, which is the
        # exact failure `all` exists to remove, rebuilt inside the fix.
        return None, [], None
    if status not in reference.ALLOWED_STATUSES:
        return None, None, refusals.refuse(
            400,
            f"status must be one of "
            f"{sorted(reference.ALLOWED_STATUSES | {reference.STATUS_ALL})}",
            got=status)
    return "d.status = ?", [status], None


def _audience_clause() -> str:
    """The `audience` membership test, in SQL, shared by `GET /reference/search`
    and `GET /reference/documents` so the two cannot answer differently about
    which documents an audience covers.

    **The search route did not have it until 2026-08-26**, and the listing route
    did: `?audience=nonsense` returned all 416 matching documents from search and
    narrowed the listing, which is contract §3.2's case 1 — an unrecognised name
    ignored, and a wider answer than the caller asked for. Measured against the
    running service before the fix. `d` is the alias both routes give the `docs`
    table; it is a literal from this file and never a request value.

    **A one-line delegation to `reference.audience_clause`, not the SQL
    itself, since issue_01M19QPV0J72EK1NDB0H8KT5EM (`crew_profiles.resolve`)
    gave the guarded json_each shape a second caller with a different
    cardinality — one bound value here, a list for a profile's
    `audience_contains` rule. `reference.py` is where the shape now lives so
    that a third caller extends it there too, rather than writing a third
    copy of the same guard.**
    """
    return reference.audience_clause(1, expr="d.audience")


def _bound_scope_clause(ident: identity.Identity, *, alias: str = ""
                        ) -> tuple[str, list] | None:
    """`(clause, params)` ready to AND into a Reference Room query, testing
    whether a `docs` row (optionally `alias`-prefixed) falls inside `ident`'s
    bound dispatch profile — or `None` when `ident` carries no binding at
    all, the signal to every caller below that it must not narrow its query
    (issue_01M35QG0VANA5P1XE3PH6A7PS4).

    **OPENS THE CONTROL DATABASE ONLY WHEN A BINDING IS PRESENT.** No
    credential minted before this existed carries one, so this costs every
    existing caller nothing — not a connection, not a query — and the
    contract stays additive under Library-Protocol 1.

    **ONE FUNCTION FOR EVERY `/reference/*` ROUTE AND THE REFERENCE HALF OF
    `GET /search`**, so the ceiling a profile draws cannot read differently
    through one door than another — the exact failure mode
    `crew_profiles.resolve` vs. a second hand-written evaluator would risk,
    which `crew_profiles.bound_scope` avoids by construction (same
    `_rule_clause` either way).
    """
    if ident.profile is None:
        return None
    conn = store.connect()
    try:
        return crew_profiles.bound_scope(conn, ident, alias=alias)
    finally:
        conn.close()


def _scoped_doc_row(conn: sqlite3.Connection, ident: identity.Identity,
                    doc_id: str, *, columns: str = "*"):
    """One `docs` row by id, or `None` when it does not exist OR falls
    outside `ident`'s bound dispatch profile — the same body, byte for byte,
    so a caller cannot tell the two apart (issue_01M35QG0VANA5P1XE3PH6A7PS4):
    a document outside the scope IS a document that does not exist, to
    every caller bound to that scope.
    """
    where, params = "id = ?", [doc_id]
    scope = _bound_scope_clause(ident)
    if scope is not None:
        scope_clause, scope_params = scope
        where += f" AND {scope_clause}"
        params += scope_params
    return conn.execute(f"SELECT {columns} FROM docs WHERE {where}",
                        params).fetchone()


def _bound_profile_mismatch(ident: identity.Identity, project: str,
                            variant: str) -> bool:
    """True when `ident` carries a dispatch-profile binding that names a
    different `<project>/<variant>` than the one given.

    **FOR A ROUTE THAT RESOLVES A PROFILE THE REQUEST NAMES, not a route that
    reads `docs` directly** (issue_01M35QG0VANA5P1XE3PH6A7PS4): `GET
    /profiles/{project}/{variant}`, `POST /bundles`, `POST /bundles/redeem`
    and `/items/{id}/checkout`'s call-slip grant each take a project and
    variant from the request (or, for redeem, from the bundle token already
    minted against one) and resolve that NAMED profile rather than filtering
    `docs` by `ident`'s own — `_bound_scope_clause` ANDs a ceiling into a
    query over `docs` and has nothing to AND into a `profiles` lookup keyed
    on the caller's own choice of project and variant. A bound identity
    naming its own profile is unaffected; naming any other gets the same
    refusal the route gives a profile that does not exist, so it learns
    nothing about what else exists. An unbound identity (`ident.profile is
    None`) is never mismatched, matching every credential minted before this
    existed.
    """
    return ident.profile is not None and ident.profile != f"{project}/{variant}"


# `binds_crews`, `binds_agents` and `binds_topics` — an `instruction-block`
# document's selector fields (issue_01M2MC21YAWT2V61QWSM247VRX). Fixed rather
# than read from the request, so `_selector_clause` below never builds SQL
# text around a caller-supplied name.
SELECTOR_FIELDS = ("binds_crews", "binds_agents", "binds_topics")


def _selector_clause(field: str) -> str:
    """The membership test for one instruction-block selector field, in SQL.

    `field` is always one of `SELECTOR_FIELDS` above — a name this file
    chose, never the raw query-parameter string — so interpolating it into
    SQL text carries no injection surface.

    **The indexer stores these three fields in `frontmatter_extra`, not a
    column of their own** — the same place `reference.doc_dict` reads an
    `agent` document's `model`, `tools` and `trigger`
    (agent-platform#517) — so the value to test is an extraction rather than
    a column, and `reference.audience_clause`'s `expr` parameter exists for
    exactly this.

    **THE EXTRACTION NEEDS ITS OWN GUARD, BEFORE `audience_clause`'S.**
    `audience_clause` guards the value it is handed against being unusable
    JSON; it does not guard the act of PRODUCING that value. `json_extract`
    itself raises `malformed JSON` on a `frontmatter_extra` that is not
    valid JSON at all — an empty string or any unparseable text, which nothing
    stops the `knowledge` repository's index from holding, since this service
    does not write that column. So `frontmatter_extra` gets the same
    json_valid/json_type nested `CASE` `audience_clause` uses, evaluated
    first: NULL out of this expression for anything that is not a JSON
    object, and only then does `json_extract` run, on input already known
    to accept it. What `audience_clause` receives is therefore always either
    NULL — matching nothing, the same answer a missing key gives — or
    whatever the key actually holds.
    """
    extraction = (
        "CASE WHEN json_valid(d.frontmatter_extra) THEN "
        "(CASE WHEN json_type(d.frontmatter_extra) = 'object' "
        f"THEN json_extract(d.frontmatter_extra, '$.{field}') ELSE NULL END) "
        "ELSE NULL END")
    return reference.audience_clause(1, expr=extraction)


@app.get("/reference/search")
async def search_reference(request: Request):
    """Full-text search over the reviewed documents, BM25-ranked.

    Deterministic, with no model anywhere on the path (ADR-0019) — the index is
    FTS5 over id, title, body and topics, and its `embeddings` table is empty. The
    same query against the same corpus returns the same ranking, which is a
    property a test can hold rather than an aspiration."""
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "reference.read"):
        return refusals.forbidden("reference.read")
    if (err := _reject_unknown_params(request, {
            "q", "limit", "offset", "kind", "project", "status",
            "audience"})):
        return err
    q = (request.query_params.get("q") or "").strip()
    if not q:
        return refusals.refuse(400, "q is required",
                               hint="pass the text to search for")
    if len(q) > 2000:
        return refusals.refuse(400, "q is too long", length=len(q))
    limit, offset, err = _page(request)
    if err:
        return err
    match = reference.build_match(q)
    if match is None:
        # EVERY TERM WAS A STOPWORD. An empty result is the honest answer and the
        # `query` echo says why — returning the whole corpus unranked, which is
        # what an unfiltered fallback would do, is the worse failure.
        return {"documents": [], "count": 0, "total": 0, "offset": offset,
                "truncated": False, "query": q,
                "note": "no searchable terms survived; every token was a stopword "
                        "or a single character"}
    where, params = [], [match]
    if (kind := request.query_params.get("kind")):
        where.append("d.kind = ?")
        params.append(kind)
    if (project := request.query_params.get("project")):
        where.append("d.project = ?")
        params.append(project)
    status_clause, status_params, err = _status_clause(request)
    if err:
        return err
    if status_clause:
        where.append(status_clause)
        params.extend(status_params)
    if (audience := request.query_params.get("audience")):
        # **THE SIBLING LISTING HAS FILTERED ON THIS SINCE PR #47 AND THIS ROUTE
        # IGNORED IT.** `GET /reference/documents?audience=x` narrowed; the same
        # parameter here returned every match, 200, with nothing saying the
        # narrowing had not happened. Both now call `_audience_clause`, so the
        # two surfaces agree about who a document is for — the same reason the
        # default status set is shared a few lines up.
        #
        # **THIS ONE FILTERS IN THE QUERY, so `total` counts the narrowed set.**
        # `/signals/search` filters after an oversampled KNN and therefore cannot
        # promise that; BM25 has no window, so a filter here is exact.
        where.append(_audience_clause())
        params.append(audience)
    # **THE BOUND PROFILE IS A CEILING, ANDED IN LAST** — a caller's own
    # `kind`/`project`/`status`/`audience` filter only narrows within it,
    # never widens past it (issue_01M35QG0VANA5P1XE3PH6A7PS4). Every
    # existing credential carries no binding and this adds nothing to its
    # query.
    scope = _bound_scope_clause(ident, alias="d")
    if scope is not None:
        scope_clause, scope_params = scope
        where.append(scope_clause)
        params.extend(scope_params)
    clause = (" AND " + " AND ".join(where)) if where else ""
    with reference.reading() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM docs_fts JOIN docs d ON d.rowid = docs_fts.rowid "
            f"WHERE docs_fts MATCH ?{clause}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT d.*, {reference.SNIPPET_SQL} AS snip, "
            # THE SCORE IS RETURNED, not merely used to order (reviewer, PR #2).
            # The contract promises "ranked document matches for a query, WITH
            # SCORES", and ordering alone gives a caller nothing to threshold on
            # or compare across queries. Shipping the ranking and dropping the
            # number would have been exactly the silent contract disagreement the
            # contract's own preamble forbids.
            f"bm25(docs_fts, {reference.BM25_WEIGHTS}) AS rank_raw "
            "FROM docs_fts JOIN docs d ON d.rowid = docs_fts.rowid "
            f"WHERE docs_fts MATCH ?{clause} "
            f"ORDER BY bm25(docs_fts, {reference.BM25_WEIGHTS}), d.id LIMIT ? OFFSET ?",
            [*params, limit, offset]).fetchall()
    return {"documents": [reference.doc_dict(r, body=False, snippet=r["snip"],
                                             score=reference.score_of(r["rank_raw"]))
                          for r in rows],
            "count": len(rows), "total": total, "offset": offset,
            "truncated": offset + len(rows) < total, "query": q}


# --- GET /search: one ranked list over all four vaults -----------------------

# `reference`'s label matches `reference.py`'s own vault name; `archives` and
# `chart-house` match `vectors.VAULT_ARCHIVES` / `vectors.VAULT_CHART_HOUSE`
# except for the hyphen, kept because every other query-string value in this
# file (`state`, `needs`, `kind`) is a bare word and a caller round-tripping a
# result's own `vault` field back into the filter should not have to know the
# vector index spells the third one with an underscore. `reviews` has no
# vector index of its own (see `search_all`'s docstring) — it is reached
# through the Archives KNN, `vectors.VAULT_ARCHIVES` directly.
SEARCH_VAULTS = ("reference", "archives", "chart-house", "reviews")

# **RECIPROCAL RANK FUSION'S OWN CONSTANT**, from the paper that introduced it
# (Cormack, Clarke & Buettcher, 2009): at 60, a rank-1 hit in a short list does
# not automatically outrank a rank-2 hit in a longer, better-populated one,
# because `1/(60+1)` and `1/(60+2)` are close where `1/(0+1)` and `1/(0+2)`
# are not.
SEARCH_RRF_K = 60

# Titles and snippets are cut to these lengths for a signal and a work item's
# body, which carry no natural excerpt of their own the way a reference
# document's FTS5 `snippet()` does. Not a hard contract, just a readable size.
SEARCH_TITLE_LEN = 120
SEARCH_SNIPPET_LEN = 240


def _search_short(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _search_vector_page(conn, table, select_cols, distance, ident, product,
                         candidate_n, wall_fn, row_to_result):
    """One vector vault's page of `/search` results, shared by the Archives
    and the Chart House halves below — same SQL shape, same two-stage
    `scoped_where`/`where` split for `filtered_out`, differing only in
    `table`, `select_cols`, an optional `wall_fn` (`_signals_wall` — the
    customer wall and the handoff wall together — for signals, `None` for
    work items, which carry no `privacy_class` and no `handoff` kind) and
    `row_to_result`, which turns a raw row into this route's response shape
    (title/snippet/type differ per vault)."""
    placeholders = ",".join("?" * len(distance))
    where = f"WHERE id IN ({placeholders})"
    params = list(distance)
    scope, scope_params = _scope_clause(ident)
    where += scope
    params += scope_params
    if wall_fn is not None:
        wall, wall_params = wall_fn(ident)
        where += wall
        params += wall_params
    scoped_where, scoped_params = where, list(params)
    if product:
        where += " AND product = ?"
        params.append(product)
    rows = conn.execute(
        f"SELECT {select_cols} FROM {table} {where}", params).fetchall()
    if product:
        scoped_count = conn.execute(
            f"SELECT COUNT(*) FROM {table} {scoped_where}",
            scoped_params).fetchone()[0]
        filtered_out = scoped_count - len(rows)
    else:
        filtered_out = 0
    ranked = sorted(rows, key=lambda r: (distance[r["id"]], r["id"])
                     )[:candidate_n]
    return [row_to_result(r) for r in ranked], filtered_out


def _search_review_page(conn, distance, ident, product, candidate_n,
                        row_to_result):
    """The Reviews vault's page — `_search_vector_page`'s twin for
    `review_records`, kept separate because that helper's `WHERE id IN (...)`
    and its distance lookup share one column, and here they do not: a
    review record is matched by `signal_id` (a key into `distance`, the
    Archives KNN this route reuses rather than re-running) but ranked and
    tie-broken by its own `id`, the same way `GET /review-records` orders
    its rows. Scoped by `_scope_clause` alone. Review records carry no
    `privacy_class` column; this assumes, rather than enforces, that a review
    record needs no customer wall. Every signal a review record links to is
    `internal` today (10,669 of 10,669, measured 2026-09-30 00:25 MST),
    `_record_review_signal` hardcodes `internal` on write, and
    `review_record_backfill.py` links only `github_review` `pr_comment`
    signals. That measurement is why no wall and no refusal are added here; a
    signal source landing at another `privacy_class` would need this
    revisited."""
    placeholders = ",".join("?" * len(distance))
    where = f"WHERE signal_id IN ({placeholders})"
    params = list(distance)
    scope, scope_params = _scope_clause(ident)
    where += scope
    params += scope_params
    scoped_where, scoped_params = where, list(params)
    if product:
        where += " AND product = ?"
        params.append(product)
    rows = conn.execute(
        f"SELECT * FROM review_records {where}", params).fetchall()
    if product:
        scoped_count = conn.execute(
            f"SELECT COUNT(*) FROM review_records {scoped_where}",
            scoped_params).fetchone()[0]
        filtered_out = scoped_count - len(rows)
    else:
        filtered_out = 0
    ranked = sorted(rows, key=lambda r: (distance[r["signal_id"]], r["id"])
                     )[:candidate_n]
    return [row_to_result(r) for r in ranked], filtered_out


@app.get("/search")
async def search_all(request: Request):
    """One ranked list over the Reference Room, the Archives, the Chart
    House and Reviews, merged by reciprocal rank fusion (`SEARCH_RRF_K`
    above).

    **EACH VAULT'S HALF GOES THROUGH EXACTLY THE CHECK ITS OWN ROUTE
    ALREADY USES, AND NEVER A SECOND IMPLEMENTATION OF IT.** Documents are
    gated on `reference.read`, the same capability `GET /reference/search`
    checks, and excluded from the default corpus by `reference.DEFAULT_
    STATUSES`, the same constant that route filters on — a document a
    status filter would already hide there is absent here too. Signals are
    gated on `signal.read` and walled by `_customer_wall` and `_handoff_wall`
    together (`_signals_wall`), the same functions `GET /signals/search`
    applies. Work items are gated on
    `item.read` and scoped by `_scope_clause`, the exact function `GET
    /items` calls. Review records are gated on `review_record.read` and
    scoped by the same `_scope_clause`, on `review_records.product` — the
    rule `GET /review-records` applies to its `path`, `doc_id` and
    `item_id` selectors and `GET /review-records/{id}` applies with
    `identity.in_scope`. **This is deliberate rather than incidental**: a
    separate, still-open task will scope every `/reference/*` read to the
    calling crew's dispatch profile, server-side, and this route calling
    the same functions those routes call is what makes it pick that change
    up by construction rather than become a second door around it.

    A vault the caller may not read for that vault's own reason (missing
    capability) is silently omitted from the merge — never a 403 for the
    whole request, since the other vaults may still answer. A vault whose
    retrieval cannot run at all (the vector index is unavailable, so
    `archives`, `chart-house` and `reviews` cannot be searched) is reported
    the same way, naming why, rather than dropped with no evidence it was
    skipped. Both kinds of omission are named in the response's
    `not_searched`.

    **REVIEWS HAS NO VECTOR INDEX OF ITS OWN.** A review record's prose is
    the body of its linked signal (`review_records.signal_id`), already
    embedded in the Archives vault — `review_record_backfill.py` linked
    10,668 of the 10,669 rows this way, and `_record_review_signal` links
    every one `POST /review-records` writes from here on. So the Reviews
    half takes its candidates from the SAME Archives KNN call the Archives
    half runs (`_search_review_page`, beside `_search_vector_page`), never
    a second KNN — matched by `signal_id` rather than by a vector of its
    own, and ranked, within that shared window, by the distance its linked
    signal scored.

    **ONE PIECE OF PROSE IS RETURNED ONCE, TO A CALLER WHO MAY READ IT AS A
    RECORD.** A signal that is some review record's linked signal is dropped
    from the Archives half's own results when the caller holds
    `review_record.read` AND `_scope_clause` admits that record's own
    `product` — whether or not `reviews` itself was one of the vaults asked
    for — since otherwise `vault=archives` alone would return the same prose
    the Reviews vault also serves. A review record's `product` need not match
    its linked signal's `product` (a repository name backfilled onto a
    crew name, in 4,468 of 10,669 linked pairs, measured 2026-09-30 00:25
    MST), so the drop applies `_scope_clause` on `review_records.product`,
    the same clause the Reviews half below uses, rather than reading the
    signal's own scope. **This is the one place `vault=archives`'s own
    results changed by this route adding Reviews**: a caller with
    `review_record.read` and scope over a linked record's product reads
    fewer Archives signals than before, each one now reachable as its review
    record through `vault=reviews` (or the merged default) instead. A caller
    without `review_record.read`, or without scope over the record's
    product, sees that signal in the Archives half exactly as it read before
    Reviews existed.

    **RANKING.** Each vault's own retrieval is unchanged — BM25 for
    documents, per ADR-0019's determinism requirement; a semantic KNN for
    signals and work items, both already embedded in the one vec0 table
    `vectors.py` shares between them, and Reviews riding that same KNN
    through its linked signal. A vault's own ranked list gives each
    row a reciprocal-rank score, `1 / (SEARCH_RRF_K + rank)`, which is what
    makes three incomparable native scores (a negative BM25 magnitude, a
    cosine distance) combinable into one ordering. Tied scores — two rows
    from different vaults ranked identically within their own lists — are
    broken by `(vault, id)`, a stable key with nothing to do with content,
    so the SAME QUERY TWICE RETURNS THE SAME ORDER (this route sits beside
    `/checkout`'s briefing on the deterministic read path ADR-0019 requires
    of it).

    **PAGING.** Each vault contributes up to `offset + limit + 1` of its
    own best-ranked rows — one more than the page needs, which is enough
    to tell whether a further page exists: reciprocal rank is a strictly
    decreasing function of a row's rank within its own vault, so no row
    fetched from a vault's OWN top `offset + limit + 1` could ever be
    outranked by one the vault fetched but did not send up. **That is a
    guarantee about ordering among what was fetched, not a guarantee that
    everything matching was fetched**, and the vaults answer it
    differently. The Reference Room's BM25 filters inside the FTS `WHERE`,
    before `LIMIT`, so what it fetches already is the filtered top-N —
    complete. The Archives, the Chart House and Reviews filter the other
    way round, the same shape `GET /signals/search` uses and for the same
    reason: `_scope_clause` (and, for the Archives, `_customer_wall`) and
    `product` run in SQL over an oversampled KNN candidate window that was
    cut by similarity alone (`vectors.OVERSAMPLE`), so a matching row
    outside that window is never fetched at all, and no filter downstream
    can recover it — a nearest-window, not a guarantee. `total` is not
    returned, matching `GET /signals/search`'s own reasoning: a fused rank
    over heterogeneous corpora has no single count that means anything,
    the same way "how many signals are semantically near this query" does
    not.

    **`kind` IS REFUSED AGAINST THE ARCHIVES, THE CHART HOUSE AND
    REVIEWS**, the same refusal `GET /signals/search` carries and for the
    same reason: every filter on those halves runs after the window
    above, so `kind` — the filter a caller reaches for to enumerate a
    rare class — would answer an empty page with `truncated: false` for a
    class that exists outside the window, not in the corpus. Refused
    rather than silently narrowed to nothing, naming `GET
    /signals?kind=...`, `GET /items?type=...` and `GET /review-records`,
    each of which filters its whole table rather than a window.
    `vault=reference` is unaffected: its `kind` filter sits inside the
    FTS `WHERE`, before `LIMIT`, and is exact.

    **`filtered_out`, per windowed vault, reports how many of that
    vault's window rows the caller's OWN `product` filter removed** — the
    same two-stage count `GET /signals/search` computes, taken after
    `_scope_clause` / `_customer_wall` and before `product` so a
    restricted caller cannot infer what the scope wall itself removed. A
    vault reading `count: 0` there beside a non-zero `filtered_out` is
    inconclusive rather than complete: the caller's own filter emptied a
    window that was not empty.
    """
    ident, err = _identify(request)
    if err:
        return err
    if (err := _reject_unknown_params(
            request, {"q", "limit", "offset", "vault", "product", "kind"})):
        return err
    limit, offset, err = _page(request)
    if err:
        return err
    q = (request.query_params.get("q") or "").strip()
    if not q:
        return refusals.refuse(400, "q is required",
                               hint="pass the text to search for")
    if len(q) > 2000:
        return refusals.refuse(400, "q is too long", length=len(q))
    vault_param = request.query_params.get("vault")
    if vault_param is not None and vault_param not in SEARCH_VAULTS:
        return refusals.refuse(
            400, f"vault must be one of {sorted(SEARCH_VAULTS)}",
            got=vault_param)
    vaults = [vault_param] if vault_param else list(SEARCH_VAULTS)
    product = request.query_params.get("product")
    kind = request.query_params.get("kind")
    if kind and vault_param != "reference":
        # **REFUSED, NOT IMPLEMENTED, AND NOT DROPPED** — the same reason
        # `GET /signals/search` refuses it, and this route's own docstring
        # above explains why: the filter would run over a similarity-cut
        # window, not the corpus. `vault_param != "reference"` catches
        # `vault=archives`/`vault=chart-house`/`vault=reviews` AND no vault
        # at all (which searches every vault, the three windowed ones
        # included).
        return refusals.refuse(
            400, "kind is not a filter on vault=archives, vault=chart-house "
                 "or vault=reviews",
            hint="use GET /signals?kind=... for archives, GET "
                 "/items?type=... for chart-house, or GET /review-records "
                 "for reviews — each filters the whole table, where this "
                 "route can only filter what the search retrieved; "
                 "vault=reference is unaffected")
    candidate_n = offset + limit + 1

    searched: list[str] = []
    not_searched: dict[str, str] = {}
    per_vault_rows: dict[str, list[dict]] = {}
    filtered_out: dict[str, int] = {}

    # --- the Reference Room: reuses reference.read, reference.build_match and
    # reference.DEFAULT_STATUSES exactly as GET /reference/search does -------
    if "reference" in vaults:
        if not identity.authorized(ident, "reference.read"):
            not_searched["reference"] = "this identity lacks 'reference.read'"
        else:
            match = reference.build_match(q)
            rows = []
            if match is not None:
                where, params = [], [match]
                if kind:
                    where.append("d.kind = ?")
                    params.append(kind)
                if product:
                    where.append("d.project = ?")
                    params.append(product)
                placeholders = ",".join("?" * len(reference.DEFAULT_STATUSES))
                where.append(f"d.status IN ({placeholders})")
                params.extend(reference.DEFAULT_STATUSES)
                # THE BOUND PROFILE IS A CEILING HERE TOO
                # (issue_01M35QG0VANA5P1XE3PH6A7PS4), the same clause `GET
                # /reference/search` ANDs in — this merge calls the same
                # query-building code that route does, which is what keeps
                # the two from ever disagreeing about what a bound
                # credential may see.
                scope = _bound_scope_clause(ident, alias="d")
                if scope is not None:
                    scope_clause, scope_params = scope
                    where.append(scope_clause)
                    params.extend(scope_params)
                clause = " AND " + " AND ".join(where)
                with reference.reading() as conn:
                    rows = conn.execute(
                        f"SELECT d.*, {reference.SNIPPET_SQL} AS snip, "
                        f"bm25(docs_fts, {reference.BM25_WEIGHTS}) AS rank_raw "
                        "FROM docs_fts JOIN docs d ON d.rowid = docs_fts.rowid "
                        f"WHERE docs_fts MATCH ?{clause} "
                        f"ORDER BY bm25(docs_fts, {reference.BM25_WEIGHTS}), "
                        "d.id LIMIT ?",
                        [*params, candidate_n]).fetchall()
            searched.append("reference")
            per_vault_rows["reference"] = [
                {"vault": "reference", "type": r["kind"], "id": r["id"],
                 "title": r["title"], "snippet": r["snip"],
                 "read_route": f"/reference/documents/{r['id']}"}
                for r in rows]

    # --- the Archives, the Chart House and Reviews: the Archives and the
    # Chart House both live in the one vec0 table vectors.py shares between
    # them; Reviews has no vector of its own and rides the Archives KNN
    # through its linked signal (`_search_review_page`, this route's own
    # docstring) --------------------------------------------------------
    vector_vaults = [v for v in ("archives", "chart-house") if v in vaults]
    needed = []
    for v in vector_vaults:
        cap = "signal.read" if v == "archives" else "item.read"
        if not identity.authorized(ident, cap):
            not_searched[v] = f"this identity lacks {cap!r}"
        else:
            needed.append(v)

    # **THE SWAP BELOW IS GATED ON THIS CAPABILITY ALONE, NEVER ON
    # `reviews_wanted`** — the docstring's "one prose once" rule changes
    # `vault=archives`'s own results for a `review_record.read` caller
    # whether or not that caller also asked for `vault=reviews`.
    review_read = identity.authorized(ident, "review_record.read")
    reviews_wanted = "reviews" in vaults
    reviews_ok = False
    if reviews_wanted:
        if not review_read:
            not_searched["reviews"] = "this identity lacks 'review_record.read'"
        else:
            reviews_ok = True

    run_archives = "archives" in needed
    # The Archives KNN runs whenever the Archives vault itself is being
    # searched, OR Reviews is searched and authorized and needs it as its
    # only source of candidates.
    fetch_archives_knn = run_archives or (reviews_wanted and reviews_ok)

    if needed or fetch_archives_knn:
        ok, why = vectors.available(store.DB_PATH)
        if not ok:
            for v in needed:
                not_searched[v] = why
            if reviews_wanted and reviews_ok:
                not_searched["reviews"] = why
        else:
            try:
                query_vector = await run_in_threadpool(vectors.embed_query, q)
            except Exception as exc:  # noqa: BLE001 — a model failure is not
                                       # a 500 the caller can act on
                print(f"search query embedding failed: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
                msg = f"the query could not be embedded ({type(exc).__name__})"
                for v in needed:
                    not_searched[v] = msg
                if reviews_wanted and reviews_ok:
                    not_searched["reviews"] = msg
            else:
                conn = vectors.connect(store.DB_PATH)
                try:
                    if fetch_archives_knn:
                        window = candidate_n * vectors.OVERSAMPLE
                        cands = vectors.nearest(conn, query_vector, window,
                                                vectors.VAULT_ARCHIVES)
                        if not cands:
                            if run_archives:
                                searched.append("archives")
                                per_vault_rows["archives"] = []
                                filtered_out["archives"] = 0
                            if reviews_wanted and reviews_ok:
                                searched.append("reviews")
                                per_vault_rows["reviews"] = []
                                filtered_out["reviews"] = 0
                        else:
                            distance = dict(cands)
                            if run_archives:
                                # **ATTACH COLD ONLY IF A CANDIDATE ID IS
                                # MISSING FROM HOT** (archives-cold-tier.md
                                # §1 row 3) — same shape `GET /signals/
                                # search` uses. A failure here degrades
                                # `archives` into `not_searched`, the same
                                # per-vault degradation this route already
                                # gives an unavailable vector index, rather
                                # than failing the whole request — the
                                # other vaults may still answer.
                                archives_table = "signals"
                                archives_ids = list(distance)
                                archives_placeholders = ",".join(
                                    "?" * len(archives_ids))
                                archives_hot_ids = {r[0] for r in conn.execute(
                                    f"SELECT id FROM signals WHERE id IN "
                                    f"({archives_placeholders})",
                                    archives_ids)}
                                if len(archives_hot_ids) < len(archives_ids):
                                    reason = store.attach_cold_or_reason(
                                        conn, readonly=True)
                                    if reason is not None:
                                        not_searched["archives"] = (
                                            f"the archive tier is "
                                            f"unavailable: {reason}")
                                    else:
                                        archives_table = (
                                            f"{store.signals_source()} AS signals")
                            if run_archives and "archives" not in not_searched:
                                # **THE WHERE IS BUILT IN TWO STAGES, THE SAME
                                # SPLIT `GET /signals/search` uses and for the
                                # same reason**: `scoped_where` ends at the
                                # boundary — scope clause and the customer
                                # wall — and `where` continues into `product`,
                                # the caller's OWN filter. `filtered_out`
                                # below counts the gap between them,
                                # answerable only because the count is taken
                                # on this side of the wall.
                                rows, filtered_out["archives"] = (
                                    _search_vector_page(
                                        conn, archives_table, "*", distance,
                                        ident, product, candidate_n,
                                        _signals_wall,
                                        lambda r: {
                                            "vault": "archives",
                                            "type": r["kind"], "id": r["id"],
                                            "title": _search_short(
                                                vectors.text_for_signal(r),
                                                SEARCH_TITLE_LEN),
                                            "snippet": _search_short(
                                                vectors.text_for_signal(r),
                                                SEARCH_SNIPPET_LEN),
                                            "read_route": f"/signals/{r['id']}"}))
                                if review_read and rows:
                                    # **ONE PROSE ONCE, SCOPED TO WHAT THE
                                    # CALLER MAY READ AS A RECORD** — drop a
                                    # signal only when it is some review
                                    # record's linked signal AND that record's
                                    # own product is inside `_scope_clause`.
                                    # A review record's product need not match
                                    # its linked signal's (a repository name
                                    # backfilled onto a crew name), so an
                                    # unscoped drop would remove a signal the
                                    # caller may read while pointing it at a
                                    # record it may not — measured on the live
                                    # database, 4,468 of 10,669 linked pairs
                                    # disagree this way. A signal whose linked
                                    # record the caller cannot read stays in
                                    # this page exactly as it would with no
                                    # Reviews vault at all.
                                    ids = [r["id"] for r in rows]
                                    placeholders = ",".join("?" * len(ids))
                                    scope, scope_params = _scope_clause(ident)
                                    linked = {
                                        row[0] for row in conn.execute(
                                            "SELECT DISTINCT signal_id FROM "
                                            "review_records WHERE signal_id "
                                            f"IN ({placeholders}){scope}",
                                            ids + scope_params)}
                                    rows = [r for r in rows
                                           if r["id"] not in linked]
                                searched.append("archives")
                                per_vault_rows["archives"] = rows
                            if reviews_wanted and reviews_ok:
                                rows, filtered_out["reviews"] = (
                                    _search_review_page(
                                        conn, distance, ident, product,
                                        candidate_n,
                                        lambda r: {
                                            "vault": "reviews",
                                            "type": "review_record",
                                            "id": r["id"],
                                            "title": _search_short(
                                                f"{r['repo_identity']} "
                                                + (f"PR #{r['pr']}"
                                                   if r["pr"] is not None else
                                                   f"@ {r['reviewed_ref']}")
                                                + f" — {r['verdict']}",
                                                SEARCH_TITLE_LEN),
                                            "snippet": _search_short(
                                                r["prose"],
                                                SEARCH_SNIPPET_LEN),
                                            "read_route":
                                                f"/review-records/{r['id']}"}))
                                searched.append("reviews")
                                per_vault_rows["reviews"] = rows
                    if "chart-house" in needed:
                        window = candidate_n * vectors.OVERSAMPLE
                        cands = vectors.nearest(conn, query_vector, window,
                                                vectors.VAULT_CHART_HOUSE)
                        if not cands:
                            searched.append("chart-house")
                            per_vault_rows["chart-house"] = []
                            filtered_out["chart-house"] = 0
                        else:
                            distance = dict(cands)
                            rows, filtered_out["chart-house"] = (
                                _search_vector_page(
                                    conn, "issue_items", store.ITEM_COLUMNS,
                                    distance, ident, product, candidate_n,
                                    None,
                                    lambda r: {
                                        "vault": "chart-house",
                                        "type": r["type"], "id": r["id"],
                                        "title": r["title"],
                                        "snippet": _search_short(
                                            r["body"], SEARCH_SNIPPET_LEN),
                                        "read_route": f"/items/{r['id']}"}))
                            searched.append("chart-house")
                            per_vault_rows["chart-house"] = rows
                finally:
                    conn.close()

    fused = []
    for rows in per_vault_rows.values():
        for rank, row in enumerate(rows, start=1):
            fused.append({**row, "score": round(1.0 / (SEARCH_RRF_K + rank), 6)})
    fused.sort(key=lambda r: (-r["score"], r["vault"], r["id"]))
    truncated = len(fused) > offset + limit
    page = fused[offset:offset + limit]
    return {"results": page, "count": len(page), "offset": offset,
            "truncated": truncated, "searched": sorted(searched),
            "not_searched": not_searched, "filtered_out": filtered_out,
            "query": q}


@app.get("/reference/topics")
async def reference_topics(request: Request):
    """The topic vocabulary in use, with how many documents carry each.

    Registered BEFORE `/reference/documents/{id}` matters not at all here — the
    paths do not collide — but the COUNT does: a topic on one document and a topic
    on ninety are different facts, and a bare list makes them look alike."""
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "reference.read"):
        return refusals.forbidden("reference.read")
    if (err := _reject_unknown_params(request, set())):
        return err
    # **THE BOUND PROFILE IS A CEILING ON WHICH DOCUMENTS' TOPICS COUNT HERE**
    # (issue_01M35QG0VANA5P1XE3PH6A7PS4), ANDed into the inner subquery below
    # — unaliased, matching that subquery's own bare `docs` — so a topic
    # that exists only on a document outside the bound profile is absent
    # rather than counted. `None` (no binding) adds nothing to the query.
    scope = _bound_scope_clause(ident)
    scope_sql = f" AND {scope[0]}" if scope is not None else ""
    scope_params = scope[1] if scope is not None else []
    with reference.reading() as conn:
        # **`json_valid` GUARDS THE JOIN, for the same reason the audience
        # filter below carries one** (security-reviewer, PR #47). A single row
        # whose `topics` column is non-NULL and unparseable makes `json_each`
        # abort the whole statement with `malformed JSON` — SQLite does not skip
        # the bad row — so one bad document takes this route down for every
        # caller. The index is written by the `knowledge` repository and a row
        # that never passed `/reference/admission` is a real possibility rather
        # than a hypothetical.
        #
        # Pre-existing rather than introduced here, and fixed alongside the new
        # filter because a guard on one of two identical joins is not a fix.
        # **THE ARRAY-SHAPED ROWS ARE SELECTED IN A SUBQUERY, so nothing
        # unparseable or wrongly-shaped reaches `json_each` at all.** A `WHERE ... AND json_valid(d.topics)`
        # alongside the join happens to work today, but it depends on the query
        # planner applying that predicate before evaluating the table-valued
        # function, which nothing guarantees. Filtering first is structural
        # rather than incidental, and it is the same reason the audience filter
        # below uses `CASE WHEN` instead of `AND`.
        rows = conn.execute(
            "SELECT je.value AS topic, COUNT(*) AS n FROM "
            "(SELECT topics FROM docs WHERE status IN "
            f"({','.join('?' * len(reference.DEFAULT_STATUSES))}) "
            "AND (CASE WHEN json_valid(topics) "
            f"THEN json_type(topics) = 'array' ELSE 0 END){scope_sql}) "
            "d, json_each(d.topics) je "
            "GROUP BY je.value ORDER BY n DESC, topic",
            [*reference.DEFAULT_STATUSES, *scope_params]).fetchall()
    return {"topics": [{"topic": r["topic"], "documents": r["n"]} for r in rows],
            "count": len(rows)}


@app.get("/reference/documents")
async def list_reference_documents(request: Request):
    """Enumerate the reviewed documents, without searching for them.

    **The Reference Room could be searched and not listed, which is why this
    exists.** `GET /reference/search` answers "which documents match these
    words" and `GET /reference/topics` answers "what subjects exist"; neither
    answers "what is in here". A client that wants the set — to render an index,
    or to stop opening the knowledge repository's index file itself — had no
    route and had to read that file directly.

    **It serves the fleet-wide view and the single-crew view from one route**
    (the operator, 2026-08-22): omit `project` and every document comes back, pass one and
    the answer narrows to that crew. Both callers exist — an operator surface
    wants everything, a crew's own cockpit wants its own shelf — and giving them
    separate routes would mean two paginations, two filter vocabularies and two
    places for the entitlement rule to drift.

    **`audience` filters on membership, not equality.** It is a JSON array on
    each document, so `?audience=team` matches any document listing `team` among
    its audiences rather than only those whose audience is exactly `["team"]`.
    That is the semantics a caller wants and the one a naive `=` gets wrong.

    **`binds_crews`, `binds_agents` and `binds_topics` filter the same way,
    on the same membership semantics, and answer a different question.**
    `audience` says who MAY READ a document; these three say who MUST OBEY
    it — an `instruction-block` document's selector fields
    (issue_01M2MC21YAWT2V61QWSM247VRX). The operator reads every document and obeys
    none of them, so one field cannot carry both meanings. `?binds_agents=
    reviewer` matches any block listing `reviewer` among its agents, the
    same any-of semantics `audience` has. Several selector parameters passed
    together AND, matching how `kind`, `project`, `status` and `audience`
    already compose — a caller narrows on every filter it names, not just
    one.

    **Not product-scoped, matching every other Reference Room route.**
    `reference.py` states the Reference Room is deliberately not scoped the way
    the Chart House and Archives are, so `project` here is a filter the caller
    chooses rather than a wall the identity imposes. `doc_dict` returns
    `audience` on every row, so a client applying its own entitlement rule has
    what it needs to do so.

    **Bodies are omitted.** A listing over ~700 documents that inlined every body
    would be megabytes for a caller that wanted titles; `GET
    /reference/documents/{id}` is the route for one document's text.

    **Each row carries `blurb` and `body_len` in the body's place**
    (issue_01M0R80SMQC575NPYPGWPS3WME), both derived from the stored body the
    query already reads. Orient's `/memory` renders every row on every request
    and needs a one-liner and a size for each; without these it fetched every
    body, ~2.4s per render. `reference.doc_dict` documents what each measures.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "reference.read"):
        return refusals.forbidden("reference.read")
    if (err := _reject_unknown_params(request, {
            "limit", "offset", "kind", "project", "status", "audience",
            *SELECTOR_FIELDS})):
        return err
    limit, offset, err = _page(request)
    if err:
        return err
    where, params = [], []
    for field in ("kind", "project"):
        if (value := request.query_params.get(field)):
            where.append(f"d.{field} = ?")
            params.append(value)
    # SAME HELPER AS SEARCH, so the two surfaces answer about the same corpus.
    # A listing that quietly included retired documents where search excludes
    # them would make the two disagree about what exists — and the default was
    # already shared by copy, which is the arrangement that lets one of them
    # drift the day somebody edits only the other.
    status_clause, status_params, err = _status_clause(request)
    if err:
        return err
    if status_clause:
        where.append(status_clause)
        params.extend(status_params)
    if (audience := request.query_params.get("audience")):
        where.append(_audience_clause())
        params.append(audience)
    # THE BOUND PROFILE IS A CEILING, ANDED IN LAST (issue_01M35QG0VANA5P1XE3PH6A7PS4):
    # a caller's own filters only narrow within it. `None` (no binding) adds
    # nothing to the query, matching every credential that exists today.
    scope = _bound_scope_clause(ident, alias="d")
    if scope is not None:
        scope_clause, scope_params = scope
        where.append(scope_clause)
        params.extend(scope_params)
    with reference.reading() as conn:
        # THE COLUMN IS CHECKED BEFORE THE CLAUSE IS BUILT, because naming a
        # column the table lacks is a PARSE error: an index predating
        # agent-platform#517 has no `frontmatter_extra`, and
        # `sqlite3.OperationalError: no such column` would 500 this route for
        # every caller rather than returning nothing. `doc_dict`'s read path
        # already treats that index as a real state and this filter must too
        # (reviewer, PR #122).
        #
        # Absent column means NO document can carry a selector field, so the
        # honest answer to "which blocks bind this agent" is none — a literal
        # false, matching nothing, rather than an error. It takes no bound
        # parameter, which is what keeps `params` aligned with the clause.
        selectors_readable = reference.has_frontmatter_extra(conn)
        for field in SELECTOR_FIELDS:
            if (value := request.query_params.get(field)):
                if selectors_readable:
                    where.append(_selector_clause(field))
                    params.append(value)
                else:
                    where.append("0")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(
            f"SELECT COUNT(*) FROM docs d{clause}", params).fetchone()[0]
        # ORDERED BY id, which is a stable total order because ids are unique.
        # An unordered listing may return the same row on two pages and skip
        # another, and a caller paging to completion would never see it happen.
        rows = conn.execute(
            f"SELECT d.* FROM docs d{clause} ORDER BY d.id LIMIT ? OFFSET ?",
            [*params, limit, offset]).fetchall()
    return {"documents": [reference.doc_dict(r, body=False, summary=True)
                          for r in rows],
            "count": len(rows), "total": total, "offset": offset,
            "truncated": offset + len(rows) < total}


@app.get("/reference/documents/{doc_id:path}/checkout")
async def reference_checkout_status(doc_id: str, request: Request):
    """Whether a document is checked out, without attempting to check it out.

    **REGISTERED BEFORE `GET /reference/documents/{doc_id:path}`, AND THAT
    ORDERING IS THE WHOLE ROUTE.** A KB document id contains slashes
    (`platform/decisions/0048-...`), so the catch-all below matches
    `.../checkout` as part of an id. Registered after it, this route is never
    reached and the failure looks like a 404 on a document that exists — dead
    code that looks alive, the same trap `GET /signals/acks` carries a warning
    about.

    **A read that costs nothing, which is why it is a separate route rather than
    a flag on the POST.** `convivy-deck` #125 removed a viewer that fetched a
    document with `agentctl kb checkout`, taking a four-hour write lease as a
    side effect and firing a detached release to drop a lease it never wanted.
    That is what a client does when the read-only answer does not exist
    (`issue_01M0JG8ZRNNW4W1DMKE3SF012V`).

    `checkout` is `null` when nothing holds the document. A dead checkout is
    still reported rather than hidden, with `expires_at` in the past, because
    "held by a session that went away" and "free" are different facts to a
    caller deciding whether to take it — and `take` treats them differently too.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "reference.read"):
        return refusals.forbidden("reference.read")
    if (err := _reject_unknown_params(request, set())):
        return err
    doc_id = store.clip(doc_id, store.MAX_DOC_ID)
    with reference.reading() as conn:
        found = _scoped_doc_row(conn, ident, doc_id, columns="id")
    # 404 ON AN UNINDEXED ID, matching `get_reference_document` and the checkout
    # POST. Answering `checkout: null` for a document that does not exist would
    # report "free" for something nobody can ever take. An id outside the
    # caller's bound profile answers the identical 404, never this status.
    if found is None:
        return refusals.not_found()
    conn = store.connect()
    try:
        held = reference_checkout.live_checkout_on(conn, doc_id)
    finally:
        conn.close()
    return {"doc_id": doc_id,
            "checkout": reference_checkout.checkout_dict(held) if held else None,
            "policy": lease.policy()}


@app.get("/reference/documents/{doc_id:path}")
async def get_reference_document(doc_id: str, request: Request):
    """One reviewed document, full text.

    `{doc_id:path}` because a document id CONTAINS SLASHES — `platform/decisions/
    0022-...` is the id, not a route hierarchy. Without the converter the router
    would 404 every real id and only ever serve documents nobody has."""
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "reference.read"):
        return refusals.forbidden("reference.read")
    if (err := _reject_unknown_params(request, set())):
        return err
    with reference.reading() as conn:
        row = _scoped_doc_row(conn, ident, doc_id)
    # 404 rather than 403, matching every other read on this service: a 403 would
    # confirm the document exists to an identity that may not know it does. A
    # document outside the caller's bound dispatch profile answers the exact
    # same 404 as one that does not exist (issue_01M35QG0VANA5P1XE3PH6A7PS4).
    if row is None:
        return refusals.not_found()
    return reference.doc_dict(row, body=True)


@app.post("/reference/admission")
async def check_reference_admission(request: Request):
    """May this Reference Room document change land — the Library's half of
    the write split (ADR-0022, `issue_01M08YECXZ68NG1WWHG0NRD086`).

    **`agentctl` calls this before it pushes; the Library never touches the
    push itself.** It answers two questions, both evaluated on every request.
    Does the document's frontmatter validate, matching the checks
    `knowledge/scripts/validate.py` calls ERRORS (see `reference_admission.py`).
    And does the named `owner` hold a live checkout on the document. A caller
    sends the document's already-parsed frontmatter as a JSON object; this
    route never parses YAML.

    **A request that names no `owner` is refused, naming the checkout
    clause** — there is no frontmatter-only path
    (`issue_01M19QW42CFK7TDZCKWK18ECY5`). `clauses_checked` on the response
    still says which clauses actually ran. Naming an `owner` additionally
    requires `reference.read`, because evaluating the clause discloses whether
    an arbitrary id is checked out.

    **Not human-reserved.** This is a deterministic schema check, not an
    authorization only a human may grant — every `agent`/`system` identity
    holding `reference.admit` may ask it, the same shape `signal.create`
    already has.

    **Not product-scoped**, matching every other Reference Room route:
    `reference.py`'s docstring states the Reference Room is deliberately not
    scoped the way the Chart House and Archives are, and an admission check
    over a document that will itself be unscoped on read should not gain a
    boundary the read side does not have.

    **`audience` and `binds_agents` are checked against the roster
    `GET /roster` currently serves** (`issue_01M34SYVH2M48K2S0FQK3K6REY`,
    the operator's ruling, 2026-10-11), read fresh here rather than cached, so a name
    `PUT /roster/{name}` just added is admitted on the very next request with
    nothing restarted. `_roster_names` only runs the query when
    `frontmatter_needs_roster` says either field is actually present, so a
    document naming neither costs no round trip.

    **`binds_agents` additionally refuses a group or the operator's own
    marker**, read fresh from the operator config by `_operator_marker_for`
    only when `binds_agents` names at least one string
    (`reference_admission.binds_agents_needs_operator_marker`); a config
    that cannot be read refuses `binds_agents` outright, naming the cause,
    the same fail-closed shape an empty roster gets.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "reference.admit"):
        return refusals.forbidden("reference.admit")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    if "frontmatter" not in payload:
        return refusals.refuse(400, "frontmatter is required")
    catalogue = reference_admission.catalogue_for_frontmatter(payload["frontmatter"])
    roster = (_roster_names()
              if reference_admission.frontmatter_needs_roster(payload["frontmatter"])
              else None)
    marker, marker_errors = _operator_marker_for(payload["frontmatter"])
    errors = reference_admission.validate_frontmatter(
        payload["frontmatter"], catalogue, roster, marker) + marker_errors
    checked = [reference_admission.CLAUSE_FRONTMATTER]
    refused = [reference_admission.CLAUSE_FRONTMATTER] if errors else []

    # THE CHECKOUT CLAUSE NOW RUNS ON EVERY REQUEST
    # (issue_01M19QW42CFK7TDZCKWK18ECY5). An absent or null `owner` is refused
    # here directly, rather than handed to `_claim_owner`: that helper's
    # fallback to the token identity exists for callers that coordinate as
    # themselves (the checkout/checkin/heartbeat routes), and the caller here
    # is a SERVICE — Orient asks on a person's behalf while the checkout is
    # held by that person. Falling back would look for a checkout held by
    # Orient itself and refuse every save its editor makes, for the wrong
    # reason, while `clauses_checked` claimed the clause ran honestly. Refusing
    # outright and naming the clause is the honest answer instead, so
    # `_claim_owner` is not called on this route at all.
    raw_owner = payload.get("owner")
    if raw_owner is None:
        checked.append(reference_admission.CLAUSE_CHECKOUT)
        refused.append(reference_admission.CLAUSE_CHECKOUT)
        errors = errors + [
            "no owner was named; the checkout clause requires one"]
    else:
        if not isinstance(raw_owner, str) or not raw_owner.strip():
            return refusals.refuse(400, "owner must be a non-empty string")
        owner = store.clip(raw_owner.strip(), store.MAX_ACTOR)
        fm = payload["frontmatter"]
        doc_id = fm.get("id") if isinstance(fm, dict) else None
        # NO 400 FOR AN UNUSABLE id. A frontmatter content problem is answered
        # `200 {may_land: false}` here, never a request-shape error — the
        # invariant `test_non_dict_frontmatter_is_a_may_land_false_not_a_400`
        # exists to hold. An id that cannot be looked up is always a frontmatter
        # failure now that a non-string id is a validation error, so `errors` is
        # already non-empty and the answer is already a refusal; the clause is
        # left unrun and `clauses_checked` says so rather than claiming a check
        # that could not happen.
        # THE WHOLE CLAUSE IS GATED ON `reference.read`, not merely the holder's
        # name. Withholding the name still leaves "no live checkout is held on X"
        # distinguishable from "checked out to another holder", which tells a
        # caller that an arbitrary id is real and someone is editing it — the
        # thing the dedicated status route denies by requiring `reference.read`
        # AND 404ing an unindexed id. This route cannot copy the 404, because a
        # write may legitimately create a document that is not indexed yet, so
        # it copies the capability check instead. Today every role holding
        # `reference.admit` also holds `reference.read`, which makes this
        # unreachable and is the role table's doing rather than this code's.
        if not identity.authorized(ident, "reference.read"):
            return refusals.forbidden("reference.read")
        if isinstance(doc_id, str) and doc_id.strip():
            doc_id = store.clip(doc_id.strip(), store.MAX_DOC_ID)
            held_by = _live_checkout_holder(doc_id)
            clause_errors = reference_admission.check_checkout(
                doc_id, held_by=held_by, owner=owner)
            checked.append(reference_admission.CLAUSE_CHECKOUT)
            if clause_errors:
                refused.append(reference_admission.CLAUSE_CHECKOUT)
                errors = errors + clause_errors
    return {"may_land": not errors, "errors": errors,
            "clauses_checked": checked, "clauses_refused": refused}


# The one clause promotion adds over admission, named beside the other two so a
# response can say which clauses ran the same way `/reference/admission` does.
CLAUSE_AGENT_DEFINITION = "agent_definition"


@app.post("/reference/promotions")
async def promote_reference_document(request: Request):
    """Admit an agent definition into the Reference Room WITHOUT a pull request
    — the epic's one non-PR path in (issue_01M19QQZ76GSZYNH11JBDMA2EM,
    ADR-0048).

    **This route authorizes; it does not write.** ADR-0022 keeps the Reference
    Room's content as files in the `knowledge` repository, and the Bosun
    commits, exactly as it does for an ordinary admission. So a promotion is
    still a git commit with a real author and a real diff — what it skips is
    the pull request, not the record. The response carries `acting_human` so
    the Bosun can put the person who authorized it in that commit rather than
    the client that called on their behalf.

    **TWO STRUCTURAL CONTROLS, AND NEITHER IS A CHECK ON WHAT THE CALLER SAYS
    IT IS DOING.** `reference.promote` is absent from `agent` and `system` at
    any protocol version, so no agent credential can reach this route at all;
    and the route refuses any identity that is not `approved_client`
    unconditionally, which is unreachable while the capability table is right
    and is kept for the reason `machine.write` states beside it — a table
    correctly configured today is not a guarantee it stays that way.

    **The item asked for `if ident.kind != "human"` and that would have built a
    route nobody could call.** `identity.resolve()` never produces `human`:
    ADR-0038 settled that no human calls the Library directly, so the
    credential reaching a human-reserved capability is the approved client that
    already verified them. Gating on a kind the service cannot produce is the
    dead-capability shape a reviewer caught in PR #1 — a refusal that looks
    like a control and is an outage. The refusal here is the credential
    boundary instead, which is what ADR-0048 is actually protecting.

    **It admits an AGENT DEFINITION and refuses everything else.** The epic
    authorizes one document class to skip review; a route taking any `kind`
    would be a general bypass of the pull request for the whole Reference Room,
    which is a much larger thing than was decided. `kind: agent` is checked as
    a clause rather than as a 400, matching how this pair of routes answers
    every other content problem.

    **A promotion is validated exactly as hard as a pull request would be.**
    The frontmatter runs the same `reference_admission.validate_frontmatter`
    clause `POST /reference/admission` runs, so skipping review cannot also
    skip the schema — a document that a PR would have rejected is refused here
    too, and `clauses_checked` says which clauses ran.

    **No checkout clause.** A promotion moves a definition that has been proven
    out in the Archives into the Reference Room, so there is no live Reference
    Room checkout to hold on a document that is not there yet. Admission's
    checkout clause is for editing a document that already exists; this route
    says so rather than silently omitting it.

    **`audience`/`binds_agents` run against the live roster, the same as
    `POST /reference/admission`** — see that route's docstring; an agent
    definition promoted with an unrostered value in either field is refused
    on the frontmatter clause exactly as a pull request would be.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "reference.promote"):
        return refusals.forbidden("reference.promote")
    if ident.kind != "approved_client":
        # UNREACHABLE TODAY — `reference.promote` is absent from every other
        # role, so the capability check above already refused it. Kept anyway,
        # matching `_machine_write_request`'s own reasoning and `write_policy`'s
        # before it: the route-level refusal holds even if the capability table
        # is ever misconfigured, and this is the route where that matters most,
        # because what it bypasses is review itself.
        return refusals.forbidden("reference.promote")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    human, attest_err = identity.attested_human(ident, payload)
    if attest_err:
        return refusals.refuse(403, attest_err["error"])
    if "frontmatter" not in payload:
        return refusals.refuse(400, "frontmatter is required")

    fm = payload["frontmatter"]
    catalogue = reference_admission.catalogue_for_frontmatter(fm)
    roster = (_roster_names()
              if reference_admission.frontmatter_needs_roster(fm) else None)
    marker, marker_errors = _operator_marker_for(fm)
    errors = reference_admission.validate_frontmatter(
        fm, catalogue, roster, marker) + marker_errors
    checked = [reference_admission.CLAUSE_FRONTMATTER]
    refused = [reference_admission.CLAUSE_FRONTMATTER] if errors else []
    # RUN EVEN WHEN THE FRONTMATTER ALREADY FAILED, so a caller sees both
    # reasons at once rather than fixing the schema and then learning the
    # document was never eligible. `fm` may be any JSON value here — a non-dict
    # is already a frontmatter error, and reading `kind` off it must not raise.
    kind = fm.get("kind") if isinstance(fm, dict) else None
    checked.append(CLAUSE_AGENT_DEFINITION)
    if kind != "agent":
        refused.append(CLAUSE_AGENT_DEFINITION)
        errors = errors + [
            f"promotion admits an agent definition; kind is {kind!r}"]
    return {"may_land": not errors, "errors": errors,
            "clauses_checked": checked, "clauses_refused": refused,
            "acting_human": human, "attested_by": ident.actor}


def _roster_names() -> frozenset[str]:
    """The names `GET /roster` would answer right now, read fresh rather than
    cached — a name `PUT /roster/{name}` just added must be admitted on the
    very next request with nothing restarted, the same freshness that route
    itself has by reading the table on every call."""
    conn = store.connect()
    try:
        return frozenset(row["name"] for row in crew_profiles.roster(conn))
    finally:
        conn.close()


def _operator_marker_for(fm) -> tuple[str | None, list[str]]:
    """The operator marker to pass into `validate_frontmatter`, read fresh
    only when `binds_agents` would actually be compared against it
    (`reference_admission.binds_agents_needs_operator_marker`) — an
    `audience`-only document costs no read, matching `_roster_names`'s own
    gating.

    Fails closed the same shape an empty roster does: when the config
    cannot be read, `binds_agents` is refused outright, naming the cause,
    rather than validated against a marker this request never had."""
    if not reference_admission.binds_agents_needs_operator_marker(fm):
        return None, []
    try:
        return operator_marker.read_marker(), []
    except operator_marker.OperatorMarkerUnavailable:
        return None, ["binds_agents: the operator marker could not be read"]


def _live_checkout_holder(doc_id: str) -> str | None:
    """The holder of the live, undead checkout on `doc_id`, or None.

    A DEAD checkout reads as None rather than as a holder, matching every other
    reader of `live_checkout_on`: the row still occupies the document, and the
    next writer is entitled to take it.
    """
    conn = store.connect()
    try:
        row = reference_checkout.live_checkout_on(conn, doc_id)
    finally:
        conn.close()
    if row is None or lease.is_dead(row):
        return None
    return row["holder"]


def _checkout_refusal(conflict) -> dict:
    """The refusal body for a checkout a live document checkout turned away.

    Matches `_lease_refusal`'s shape: names the holder and the expiry rather
    than a bare "no", and the same hint about who may break a live one.
    """
    held = conflict.checkout
    until = store._iso(lease.expires_at(held["taken_at"], held["heartbeat_at"]))
    return {
        "error": f"already checked out: {held['doc_id']} is held by "
                 f"{held['holder']} until {until}",
        "checkout": reference_checkout.checkout_dict(held),
        "hint": "an agent may reclaim a checkout whose holder is gone, but "
                "only a human may break a live one, and no route here does "
                "that",
    }


@app.post("/reference/documents/{doc_id:path}/checkout")
async def checkout_reference_document(doc_id: str, request: Request):
    """Check out a Reference Room document — the lock, not the write.

    `issue_01M0DVAH9KAACHH0RRWQZ3KZTE`. The document itself stays a file in the
    `knowledge` repository (ADR-0022); this takes a lease on its id, refusing a
    second concurrent checkout the same way `/items/{id}/checkout` refuses a
    second lease on a live epic. `POST /reference/admission` requires one: it
    refuses any write whose named `owner` does not hold a live checkout here,
    and any request that names no `owner` at all.

    404s against the knowledge index before taking anything, matching
    `get_reference_document`: a caller checking out an id nothing indexes is
    told so rather than handed a lease on a document that does not exist.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "reference.checkout"):
        return refusals.forbidden("reference.checkout")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    holder, err = _claim_owner(payload, ident)
    if err:
        return err
    run_id = payload.get("run_id")
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        return refusals.refuse(400, "run_id must be a non-empty string")
    run_id = store.clip(run_id.strip(), 100) if run_id else None
    # BREAKING A LIVE CHECKOUT IS A HUMAN-ONLY ACT, matching `/items/{id}/
    # checkout` for the same reason: this service issues no human identity, so
    # `force` is refused outright rather than silently ignored.
    if payload.get("force"):
        return refusals.refuse(
            403, "only a human may break a live checkout",
            hint="a dead checkout needs no force — retry without it; this "
                 "service issues no human identity until the Library mints "
                 "its own tokens")
    doc_id = store.clip(doc_id, store.MAX_DOC_ID)
    with reference.reading() as conn:
        found = _scoped_doc_row(conn, ident, doc_id, columns="id")
    # 404 rather than 403, matching `get_reference_document`: a 403 would
    # confirm the document exists to an identity that may not know it does. A
    # document outside the caller's bound dispatch profile answers the same
    # 404 rather than a lease on a document it may never read
    # (issue_01M35QG0VANA5P1XE3PH6A7PS4).
    if found is None:
        return refusals.not_found()
    try:
        result = await run_in_threadpool(_do_checkout_document, ident, doc_id,
                                         holder, run_id)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    status, body = result
    return JSONResponse(status_code=status, content=body)


def _do_checkout_document(ident, doc_id: str, holder: str, run_id):
    conn = store.connect_rw()
    try:
        try:
            held = reference_checkout.take(conn, doc_id, holder=holder,
                                           actor=ident.actor, run_id=run_id)
        except reference_checkout.CheckoutConflict as conflict:
            conn.rollback()
            return 409, _checkout_refusal(conflict)
        conn.commit()
        return 200, {"checkout": reference_checkout.checkout_dict(held),
                     "policy": lease.policy()}
    finally:
        conn.close()


@app.post("/reference/documents/{doc_id:path}/checkin")
async def checkin_reference_document(doc_id: str, request: Request):
    """Hand a Reference Room document's checkout back.

    Scoped to the holder, matching `/items/{id}/release`: a session hands back
    what it holds, and `force` is refused outright — this service issues no
    human identity, so breaking a checkout that is not yours is not a call any
    token here can make.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "reference.checkout"):
        return refusals.forbidden("reference.checkout")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    holder, err = _claim_owner(payload, ident)
    if err:
        return err
    if payload.get("force"):
        return refusals.refuse(
            403, "only a human may force-check-in a checkout that is not "
                 "yours",
            hint="this service issues no human identity until the Library "
                 "mints its own tokens; check-in without force returns what "
                 "you hold")
    doc_id = store.clip(doc_id, store.MAX_DOC_ID)
    try:
        result = await run_in_threadpool(_do_checkin_document, ident, doc_id,
                                         holder)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    status, body = result
    return JSONResponse(status_code=status, content=body)


def _do_checkin_document(ident, doc_id: str, holder: str):
    conn = store.connect_rw()
    try:
        held = reference_checkout.live_checkout_on(conn, doc_id)
        if held is None:
            return 409, {"error": "not checked out"}
        if held["holder"] != holder:
            return 409, {"error": f"checkout held by {held['holder']}"}
        released = reference_checkout.release(conn, doc_id, holder=holder,
                                              actor=ident.actor)
        conn.commit()
        return 200, {"checkout": reference_checkout.checkout_dict(released)}
    finally:
        conn.close()


@app.post("/reference/documents/{doc_id:path}/heartbeat")
async def heartbeat_reference_document(doc_id: str, request: Request):
    """Say you are still holding this document's checkout.

    **A GUI editing session is the shape this exists for**
    (`issue_01M0JG924BJ3EJDHWEXBM73A4X`). A worker that keeps calling the
    Library heartbeats itself on every lease-bearing action, so it never needs
    this. A human editing a document in Deck makes no calls at all while they
    type, holds a real checkout, and looks identical to a session that died —
    so the checkout lapses mid-edit and somebody else can take the document out
    from under them.

    **404 when the caller holds no live checkout, rather than taking one.** A
    heartbeat that could CREATE a checkout would be a checkout by another name,
    and one that skipped the conflict check — the same rule
    `/items/{id}/heartbeat` states and for the same reason.

    **A dead checkout is refused rather than revived.** Past its grace the
    document is already reclaimable, and reviving it on a heartbeat would reopen
    the window a reclaim just closed. The holder that went quiet long enough has
    to check out again, which is a refusal it can act on rather than a silent
    race it cannot see.

    Refreshing never extends past the hard cap: `heartbeat_at` moves and
    `taken_at` does not, so a session cannot hold a document indefinitely by
    keeping quiet and beating.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "reference.checkout"):
        return refusals.forbidden("reference.checkout")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    holder, err = _claim_owner(payload, ident)
    if err:
        return err
    doc_id = store.clip(doc_id, store.MAX_DOC_ID)
    try:
        result = await run_in_threadpool(_do_heartbeat_document, doc_id, holder)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    status, body = result
    return JSONResponse(status_code=status, content=body)


def _do_heartbeat_document(doc_id: str, holder: str):
    conn = store.connect_rw()
    try:
        touched = reference_checkout.touch(conn, doc_id, holder=holder)
        if touched is None:
            # ONE MESSAGE FOR THREE CAUSES — no checkout, someone else's, or
            # dead. Distinguishing them here would tell a caller who holds a
            # document it did not ask about; `GET .../checkout` is the route
            # that answers that, and it answers it the same way to everyone.
            return 404, {
                "error": f"{holder} holds no live checkout on {doc_id}",
                "hint": "check the document out to take one; a checkout past "
                        "its grace cannot be revived by a heartbeat"}
        conn.commit()
        return 200, {"checkout": reference_checkout.checkout_dict(touched),
                     "policy": lease.policy()}
    finally:
        conn.close()


# --- the crew read surface: roster and profiles (issue_01M19QPV0J72EK1NDB0H8KT5EM) --
#
# **NONE OF THE THREE ROUTES BELOW PAGINATE, and that is a deliberate
# departure from every other listing route in this file.** `_page` exists
# because a page of an open-ended corpus is still a meaningful partial
# answer — the next page of items, or of courses — but a roster and a
# resolved profile are each a SET that is meaningless in part. A
# individuator acting on page 1 of a resolved profile would render a
# partial crew with nothing in the rendered output saying so; a caller
# validating an `audience` value against page 1 of the roster would reject a
# real name that happened to sort onto page 2. Bounding by SET SIZE rather
# than by caller-supplied `limit`/`offset` is the same reasoning
# `reference_topics` already applies to the topic vocabulary.
#
# **The bound is the corpus, not caller input, and it is measured rather
# than assumed.** Against the live index on 2026-09-06: 750 documents total;
# the largest resolved profile is `vivy/daytime` at 234 documents, about 111
# KB of references; the absolute ceiling — every document in the index,
# should a profile ever name no filter narrow enough to avoid it — is about
# 355 KB.
#
# **NOTHING CAPS A RESPONSE ON THIS SERVICE, and naming a cap here would be
# a false comfort.** `store.MAX_REQUEST_BYTES` bounds an inbound request
# body and says nothing about what a route returns; there is no response-side
# equivalent to point at. What bounds these three is the corpus itself, which
# grows by pull request into the `knowledge` repository rather than by
# anything a caller can do to this route — so the number to watch is the
# index's document count, and the day a resolved profile is large enough to
# hurt is a day someone can see coming in that count.


@app.get("/roster")
async def list_roster(request: Request):
    """Every name on the fleet roster — the names that used to be
    `ALLOWED_AUDIENCE`, a Python set literal in agent-platform's `lib/
    knowledge/knowledge.py`, read once at import by a long-lived watcher.
    `crew_registry.roster_add`/`roster_remove` are the write path; this is
    its read counterpart, gated on `roster.read` rather than `roster.write`
    so an ordinary agent or system credential can ask "who is on the
    roster" without gaining any ability to change it.

    **Empty rather than 404 or 503 when nobody has loaded the table yet.**
    `load_crew_registry.py` is an operator step run once against the live
    database (out of scope for this route); until it runs, `roster_agents`
    genuinely holds zero rows, which is a true and answerable fact about
    the table's current state, not a failure to read it.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "roster.read"):
        return refusals.forbidden("roster.read")
    if (err := _reject_unknown_params(request, set())):
        return err
    conn = store.connect()
    try:
        agents = crew_profiles.roster(conn)
    finally:
        conn.close()
    return {"agents": agents, "count": len(agents)}


@app.put("/roster/{name}")
async def add_roster_name(name: str, request: Request):
    """Add `name` to the fleet roster, or refresh it if already present.
    Orchestrator-only-write (the operator, 2026-09-04, on
    issue_01M19QP8S0QPKM0MN7XHYWNGKY: "the roster for a crew should be
    malleable and not need a pull request").

    **The capability check and the name validation both live in
    `crew_registry.roster_add`, not here** — this route calls it and
    translates whatever it raises, rather than checking `roster.write` or
    the name's shape a second time.
    """
    ident, err = _identify(request)
    if err:
        return err
    try:
        row = await run_in_threadpool(crew_registry.roster_add, ident, name)
    except crew_registry.RosterWriteForbidden:
        return refusals.forbidden("roster.write")
    except crew_registry.InvalidRosterRow as e:
        return refusals.refuse(400, str(e))
    except crew_registry.RosterNameWithheld:
        # The same 404 a missing name gets elsewhere on this service, so the
        # roster is no oracle for which names belong to black-box crews.
        return refusals.not_found()
    except crew_registry.RegistryUnreadable as e:
        return refusals.refuse(
            503, str(e),
            hint="retry once the registry at the configured path is "
                 "readable again")
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    return row


@app.delete("/roster/{name}")
async def remove_roster_name(name: str, request: Request):
    """Remove `name` from the fleet roster. 404 when `name` was not on the
    roster — the same "resolve, then act" shape every other route in this
    file uses, rather than a no-op 200 that cannot distinguish "already
    gone" from "never existed".
    """
    ident, err = _identify(request)
    if err:
        return err
    try:
        row = await run_in_threadpool(
            crew_registry.roster_remove, ident, name)
    except crew_registry.RosterWriteForbidden:
        return refusals.forbidden("roster.write")
    except crew_registry.InvalidRosterRow as e:
        return refusals.refuse(400, str(e))
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    if row is None:
        return refusals.not_found()
    return row


@app.get("/profiles")
async def list_profiles(request: Request):
    """Which per-project profiles exist — `crew_registry.profile_put`'s
    write counterpart, telling a caller what it could ask `GET /profiles/
    {project}/{variant}` for.

    **`include` never appears here, or on any of this route's three
    siblings.** A profile is a selection rule and this route answers "which
    profiles exist", not "what does each one select" — the resolved
    document set is what `GET /profiles/{project}/{variant}` exists to
    answer, precisely so no caller has to evaluate the rule itself.
    `crew_profiles.profiles()` leaves the column out of its query rather
    than trusting this route to drop it from the response, so there is one
    place this guarantee lives rather than two that could drift apart.

    **A bound identity sees only its own profile here**
    (issue_01M35QG0VANA5P1XE3PH6A7PS4), the listing counterpart of the
    refusal `GET /profiles/{project}/{variant}` gives it for any other: a
    bound caller learns nothing about which other profiles exist, and
    `count` counts only what it is served. An unbound identity lists every
    profile, as before.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "roster.read"):
        return refusals.forbidden("roster.read")
    if (err := _reject_unknown_params(request, set())):
        return err
    await run_in_threadpool(profile_sync.refresh)
    conn = store.connect()
    try:
        rows = crew_profiles.profiles(conn)
    finally:
        conn.close()
    rows = [row for row in rows if not _bound_profile_mismatch(
        ident, row["project"], row["variant"])]
    return {"profiles": rows, "count": len(rows)}


@app.get("/profiles/{project}/{variant}")
async def resolve_profile(project: str, variant: str, request: Request):
    """A profile, resolved to the document set it selects — the one route
    in this trio that evaluates a selection rule rather than listing rows.

    **THE RULE ITSELF NEVER TRAVELS ON THE WIRE — its resolved OUTPUT
    does.** This is the epic's step-4 property this whole read surface
    exists for: three clients in three languages (Orient's
    `library_client.py`, agent-platform's `vault_plane.py`, Deck's Swift)
    would each have to re-implement `crew_profiles.resolve`'s evaluation if
    this route hoped they would resolve `include` themselves, and any
    disagreement between three re-implementations would be silent. Calling
    `crew_profiles.resolve_counted`, which `crew_profiles.resolve` wraps,
    here and returning what it returns is what keeps that evaluation to one
    place.

    **Bodies are omitted, matching `GET /reference/documents`.** A grant
    names documents by id and never their content (the context-economy
    property the epic is for); the Individuator fetches each body
    separately through `GET /reference/documents/{id}`.

    **`claude_md_extras` IS returned, and that is not the same thing as
    returning the profile's rule.** The prohibition above is on the
    SELECTION rule (`include`); `claude_md_extras` is content —
    `knowledge-ctx` renders it straight into `.claude/CLAUDE.local.md` — and
    withholding it would leave a caller with no way to obtain it at all,
    since nothing else in this service serves it.

    **`rules` gives each `include` rule's own match count, by position and
    without the rule** (issue_01M34SV3BDRHBTXZPZ8C4AH9HD): `{"position": i,
    "matches": n}` in rule order, counted before the union dedupes, so
    `knowledge-ctx`'s individuation telemetry can still see a rule that
    matches nothing. The rule's text stays off the wire for the reason
    above; a reader who needs it maps the position onto the profile's file
    in the `knowledge` repository.

    404 when no such profile row exists. **A profile that exists and
    resolves to zero documents is a 200 with an empty `documents` list, a
    different fact from a profile that was never created** — collapsing the
    two into one 404 would make "nobody has written this profile yet" and
    "this profile's rules are simply too narrow today" indistinguishable to
    a caller deciding whether to write one.

    **A bound identity naming any profile but its own gets the same 404**
    (issue_01M35QG0VANA5P1XE3PH6A7PS4): this route resolves the profile the
    request names, not `ident`'s own, so a credential bound to one crew's
    profile could otherwise read another's resolved document set — exactly
    the bundle.issue-shaped bypass the binding exists to close. The check
    runs before the profile lookup, so a bound caller cannot tell "not mine"
    from "does not exist" by timing either.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "roster.read"):
        return refusals.forbidden("roster.read")
    if (err := _reject_unknown_params(request, set())):
        return err
    if _bound_profile_mismatch(ident, project, variant):
        return refusals.not_found()
    await run_in_threadpool(profile_sync.refresh)
    conn = store.connect()
    try:
        row = crew_profiles.profile_row(conn, project, variant)
    finally:
        conn.close()
    if row is None:
        return refusals.not_found()
    # An index the watcher replaced between the open and this read raises
    # in the read rather than in `connect()`; `reading()` raises it as the
    # open's failure, so the registered handler answers the same 503
    # (issue_01M229WQ78Y6AV9RPVM5JSSNJF).
    with reference.reading() as index_conn:
        docs, counts = crew_profiles.resolve_counted(row, index_conn)
    return {
        "profile": {"project": row["project"], "variant": row["variant"],
                    "updated_at": row["updated_at"],
                    "updated_by": row["updated_by"]},
        "documents": [reference.doc_dict(d, body=False) for d in docs],
        "count": len(docs),
        "rules": [{"position": i, "matches": n} for i, n in enumerate(counts)],
        "claude_md_extras": row["claude_md_extras"],
    }


def _effective_projects(person_id: str, ident: identity.Identity):
    """`({"projects": [...], "count": n}, None)` for `person_id`'s EFFECTIVE
    set — the projects `person_id` created by the registry's own record,
    plus an explicit grant, minus an explicit removal
    (issue_01M2RSFKJBEHD5Y76X0B6XAYZG) — bounded by `ident`'s own scope; or
    `(None, refusal)` when the project registry cannot be read. Shared by
    `GET /people/{person_id}/projects` and `POST /people/by-email/projects`,
    so the two answer the same question the same way.

    **Answered out of `person_products`, `person_product_removals` and the
    registry, with no lookup of `people` at all.** An unknown person and a
    person with an empty effective set must return the same response, field
    for field, including status code — the same argument `app.py`'s
    existence-probe comments make elsewhere for products and for items.
    Looking `people` up first would hand back a display name for one case
    and nothing for the other, which is the leak this helper exists not to
    have; an unknown person has no registry-recorded creations and no rows
    in either table, so it reads as an empty effective set for the same
    reason it always read as zero grants. A caller may pass a `person_id`
    that matches no row at all — SQLite's `person_id = ?` never matches a
    `NULL` parameter either, so passing `None` reads the same way.

    **`_scope_clause` (`app.py:1056`) bounds the two tables; `identity.in_scope`
    bounds the registry-derived default, since that half never reaches SQL.**
    The whole security design here is that a project outside the caller's
    own product scope is never read out of the database, and never added
    from the registry, in the first place — filtering afterward is how a
    total ends up counting a row the caller may not see, which is exactly
    the failure mode `_scope_clause`'s own docstring names. This is also why
    the answer is safe for a scoped `approved_client` credential and not
    merely safe today because the sole caller so far, `orient`, holds
    `products: ["*"]`.

    **Fails CLOSED on an unreadable registry**, unlike `projects.resolve()`
    on its own: `projects.slugs_created_by()` returns `None` rather than an
    empty list when the registry cannot be read, and this refuses rather
    than serving a default computed from no registry — the same distinction
    `_merge_repo_scope` draws for `projects.available()`. An unreadable
    registry must not read as "created nothing".
    """
    default_all = projects.slugs_created_by(person_id)
    if default_all is None:
        return None, refusals.refuse(503, "the project registry is unavailable")
    default = {p for p in default_all if identity.in_scope(ident, p)}
    scope, scope_params = _scope_clause(ident)
    conn = store.connect()
    try:
        held = {r["product"] for r in conn.execute(
            f"SELECT product FROM person_products WHERE person_id = ?{scope} "
            f"ORDER BY product",
            [person_id, *scope_params]).fetchall()}
        # A removal only ever names a default project, so an empty `default`
        # (after scoping) means nothing could be in `removed` either —
        # queried only when `default` is non-empty, which also keeps this
        # working for a person the registry names no creations for against a
        # schema predating `person_product_removals` (`store.change_person_products`
        # draws the same line).
        removed = {r["product"] for r in conn.execute(
            f"SELECT product FROM person_product_removals WHERE "
            f"person_id = ?{scope} ORDER BY product",
            [person_id, *scope_params]).fetchall()} if default else set()
    finally:
        conn.close()
    effective = sorted((default - removed) | held)
    return {"projects": effective, "count": len(effective)}, None


@app.get("/people/{person_id}/projects")
async def person_projects(person_id: str, request: Request):
    """Which projects `person_id` may see — the first slice of person
    identity in the Library (issue_01M2RSE08WRRB1RJY1ZCSMD6EG). The effective
    set itself is `_effective_projects`, shared with `POST
    /people/by-email/projects`.

    **`person_id` is its own path parameter, and `acting_human` is never
    read on this route at all.** `attested_human` exists to record who a
    client is acting on behalf of for an approved-client WRITE; this is a
    read with no write to attribute, and `person_id` names whose projects
    are being asked for, not who is asking. Reusing `acting_human` for that
    would be the unreviewed change the item's spec calls out by name:
    `attested_human` checks only that the caller is an approved client and
    that the string fits `store.MAX_ACTOR`, never that the named person
    exists or that this credential may act for them.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "person.read"):
        return refusals.forbidden("person.read")
    if (err := _reject_unknown_params(request, set())):
        return err
    result, refusal = _effective_projects(person_id, ident)
    if refusal:
        return refusal
    return result


def _project_names(payload: dict, field: str):
    """`payload[field]` as a list of product names, or `(None, refusal)`.

    Each name is proved a string before anything else touches it, then stripped,
    non-empty and within `store.MAX_IDENT`, the bound `person_products.product`
    was sized for. An absent field is an empty list.

    **The type check comes first because everything downstream HASHES these
    values**, so this is the one place that can refuse an unhashable one. Every
    `set(...)` built from these names is such a site — `set(names)` below,
    `sorted({*grant, *revoke})` and `set(grant)`/`set(revoke)` in the route and
    in `store.change_person_products`, and the frozenset membership inside
    `identity.in_scope`. It is the `set(...)` construction that raises, not the
    `&` or `-` applied afterwards. Treat that list as examples rather than a
    census: a new caller adds a site, and what keeps them all safe is that they
    are downstream of this check, not that they are enumerated here. An
    unhashable JSON value reaching any of them raises `TypeError` on an
    authenticated path, which is a 500 where a malformed body should be a 400 —
    the class `identity.py` records hitting three times.

    **Names are stored uninterpreted, and `"*"` is not special here.** It passes
    these checks like any other name and is written as the literal string.
    Nothing in this repo reads a `person_products` row as a wildcard:
    `_scope_clause` and `identity.in_scope` both test `"*"` against the CALLER's
    own products, never the row's. The row's value is the probe, not an entry in
    the list it is tested against, so three things follow — a scoped reader never
    sees such a row, because `product IN (<their products>)` cannot match `"*"`;
    a `*`-scoped reader gets no clause at all and reads it back as the literal
    `"*"`; and only a `*`-scoped administrator can write one, since
    `identity.in_scope(ident, "*")` is false for a scoped one and the route
    answers 403. **A client reading a person's project set must not treat it as a
    wildcard either** (security-reviewer, PR #135 round 2).
    """
    raw = payload.get(field, [])
    if not isinstance(raw, list):
        return None, refusals.refuse(400, f"{field} must be a list of project names")
    names = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            return None, refusals.refuse(
                400, f"{field} must hold only non-empty project names")
        name = item.strip()
        if len(name) > store.MAX_IDENT:
            return None, refusals.refuse(
                400, f"a project name in {field} is longer than "
                     f"{store.MAX_IDENT} characters")
        names.append(name)
    return sorted(set(names)), None


def _parse_email(raw) -> tuple["str | None", "JSONResponse | None"]:
    """A plausible single email address, trimmed and lower-cased, or a 400.

    **Exactly the three checks the item's spec names, no more**: one `"@"`,
    no whitespace anywhere in it, and a bounded length
    (`store.MAX_EMAIL`) — never an attempt at validating deliverability, a
    domain shape, or anything RFC 5322 would additionally require. Trimmed
    before the whitespace check runs, so a leading or trailing space is not
    itself a refusal; one appearing anywhere else in the string is.
    """
    if not isinstance(raw, str):
        return None, refusals.refuse(400, "email must be a string")
    trimmed = raw.strip()
    if not trimmed:
        return None, refusals.refuse(400, "email must not be empty")
    if any(c.isspace() for c in trimmed):
        return None, refusals.refuse(400, "email must not contain whitespace")
    if trimmed.count("@") != 1:
        return None, refusals.refuse(
            400, "email must contain exactly one '@'")
    if len(trimmed) > store.MAX_EMAIL:
        return None, refusals.refuse(
            400, f"email is longer than {store.MAX_EMAIL} characters")
    return trimmed.lower(), None


@app.post("/people", status_code=201)
async def create_person(request: Request):
    """Create a person record. Body: `{"display_name": "<string>", "email":
    "<address>"}`, `email` optional. Grant projects afterwards with `POST
    /people/{person_id}/projects` and correct the email with `PUT
    /people/{person_id}/email`.

    **GATED EXACTLY AS `POST /people/{person_id}/projects` AND `PUT
    /people/{person_id}/email` ARE, for the same reason**: `person.write`
    (approved-client credentials only) and a row in `administrators` for
    `ident.actor`. `acting_human` is never read here, for the reason those
    two routes' own docstrings give in full.

    **EVERY REFUSAL BEFORE THE BODY IS ONE RESPONSE, BYTE FOR BYTE, THE SAME
    ONE THOSE TWO ROUTES GIVE A NON-ADMINISTRATOR.** No credential, an
    unresolvable one, a credential lacking `person.write`, and a credential
    that is not an administrator's all return `refusals.not_found()`. There
    is no person named in the path for this route to 404 on finding
    missing, so the fourth byte-identical case those two routes list
    (an administrator naming a person who does not exist) has no
    counterpart here.

    **The body is read only after that gate**, so a malformed body or a
    validation 400 is seen only by a verified administrator.

    `display_name` must be a string, non-empty once stripped, and at most
    `store.MAX_DISPLAY_NAME` characters — `people.display_name`'s own
    column width (`76bfd9912739`), the same bound `bootstrap_library.py`
    holds the first administrator's name to. `email`, when given, is
    validated and normalised by the same `_parse_email` `PUT
    /people/{person_id}/email` uses, and is unique-checked inside
    `store.create_person`'s own write lock the way `set_person_email`
    checks its own — a 409 on a held email never names the other holder,
    for the reason that route's own docstring gives.

    **The person row and the email write are one transaction**
    (`store.create_person`), so a refused email leaves no person behind.

    This never creates an administrator, mints no credential, and grants no
    project: a new person starts with the default set alone (the projects
    the registry records them as having created, which for a new id is
    none).
    """
    ident, err = _identify(request)
    refused = refusals.not_found()
    if err or ident.kind != "approved_client" or not identity.authorized(
            ident, "person.write"):
        return refused
    conn = store.connect()
    try:
        allowed = store.is_administrator(conn, ident.actor)
    finally:
        conn.close()
    if not allowed:
        return refused
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    raw_name = payload.get("display_name")
    if not isinstance(raw_name, str):
        return refusals.refuse(400, "display_name must be a string")
    display_name = raw_name.strip()
    if not display_name:
        return refusals.refuse(400, "display_name must not be empty")
    if len(display_name) > store.MAX_DISPLAY_NAME:
        return refusals.refuse(
            400, f"display_name is longer than {store.MAX_DISPLAY_NAME} "
                 f"characters")
    email = None
    if payload.get("email") is not None:
        email, bad = _parse_email(payload["email"])
        if bad:
            return bad
    try:
        result = await run_in_threadpool(
            store.create_person, ident.actor, display_name, email)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    if result is None:
        # The administrator went away between the read above and the write
        # lock, so this is the refusal the read would have given.
        return refused
    if result is store.EMAIL_TAKEN:
        return refusals.refuse(409, "this email is already in use")
    return {"person_id": result["person_id"],
            "display_name": result["display_name"],
            "sign_in_email": result["email"], "created_by": ident.actor}


@app.post("/people/by-email/projects")
async def person_projects_by_email(request: Request):
    """Which projects a sign-in email's person may see — the sixth slice of
    person identity in the Library (issue_01M4QQZ7VTANJ5ZGF630DCB0Y7). Body:
    `{"email": "<address>"}`. Answers in exactly the shape `GET
    /people/{person_id}/projects` does, bounded by the CALLER's own product
    scope.

    **REGISTERED AHEAD OF `POST /people/{person_id}/projects` ON PURPOSE.**
    Both match the path shape `/people/{X}/projects` by HTTP method, and a
    router tries routes in registration order — `by-email` would otherwise
    be consumed as a `person_id` by the older, more general route and this
    one would never be reached. A test sends a real request rather than
    asserting on source order, since source order is the thing that could
    silently stop mattering.

    **The email goes in the body, never the path or the query string**, so
    it stays out of access logs the way a path parameter or a query string
    would not. `_parse_email` applies the same three checks writing one
    does, and normalises the same way, so a caller that wrote an address in
    any casing still matches the row `PUT /people/{person_id}/email` stored
    lower-cased.

    **For a person in `administrators`, the answer is every project the
    CALLER's scope reaches, computed here rather than left to the client.**
    The epic's own words are that a client must never branch on the role —
    an administrator's answer is not "all projects" unqualified, it is
    bounded by `identity.in_scope` exactly as an ordinary person's is, so a
    scoped credential asking about an administrator sees only what it could
    already see. Fails CLOSED with 503 on an unreadable registry, the same
    distinction `_effective_projects` draws for an ordinary person's default,
    because "every project this scope reaches" computed from no registry is
    not an answer this route can stand behind.

    **An email naming no person answers exactly like a person with an empty
    set**, because it falls through to `_effective_projects` with no
    `person_id` any row can match — the same `person_id = ?` comparison
    against a NULL parameter that makes an unknown string `person_id` read
    as empty on the sibling route, so this needs no extra case for it. A
    person named in `administrators` is looked up by their resolved
    `person_id` before that fallthrough, so an unknown email is never
    mistaken for an administrator's.

    **Gated exactly as `GET /people/{person_id}/projects` is**: `person.read`,
    nothing more — the credential need not be an administrator's own, since
    this is the read route Orient, Deck and the Bosun already hold, now
    reachable by email instead of by id.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "person.read"):
        return refusals.forbidden("person.read")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    email, bad = _parse_email(payload.get("email"))
    if bad:
        return bad
    conn = store.connect()
    try:
        person_id = store.person_id_by_email(conn, email)
        is_admin = person_id is not None and store.is_administrator(
            conn, person_id)
    finally:
        conn.close()
    if is_admin:
        if not projects.available():
            return refusals.refuse(503, "the project registry is unavailable")
        effective = sorted(p for p in projects.projects()
                           if identity.in_scope(ident, p))
        return {"projects": effective, "count": len(effective)}
    result, refusal = _effective_projects(person_id, ident)
    if refusal:
        return refusal
    return result


@app.post("/people/{person_id}/projects")
async def change_person_projects(person_id: str, request: Request):
    """Grant and revoke projects for `person_id` — the third slice of person
    identity in the Library (issue_01M2RSF4MMRT7PBHJJPQ2BKEC7). Body:
    `{"grant": [...], "revoke": [...]}`, either optional, at least one name in
    total; a name in both is refused as ambiguous. Read the result back with
    `GET /people/{person_id}/projects`.

    **AUTHORIZED ON THE IDENTITY THE LIBRARY VERIFIED, NEVER ON ANYTHING THE
    REQUEST SAYS.** The caller must hold `person.write` (approved-client
    credentials only, `identity.py`), and `ident.actor` must have a row in
    `administrators`. `ident.actor` is the `name` on the credential entry the
    Library found in its own file, which for a credential minted by
    `bootstrap_library.py` is a person id. The channel and what it does and does
    not establish are written up beside `person.write` in `identity.py`.
    **`acting_human` is never read here**, and a call carrying an
    administrator's name in it and no such credential is refused like any
    other (`attested_human` checks the credential kind and the string's length,
    never that the person exists or may be acted for).

    **EVERY REFUSAL BEFORE THE BODY IS ONE RESPONSE, BYTE FOR BYTE.** No
    credential, an unresolvable one, a credential lacking `person.write`, a
    credential that is not an administrator's, and an administrator naming a
    person who does not exist all return `refusals.not_found()`. Any
    difference between them (a 401 for the first, a 403 naming the capability
    for the second, a 404 for the last) would let a caller ask "is this
    credential valid", "is this person an administrator" or "does this person
    exist" and read the answer off the status. The cost is that an operator
    whose credential is wrong sees the same 404 for every cause, so this route
    is where to look first when a provisioned administrator is refused: the
    credential's `name` must equal a row in `administrators`, and its `kind`
    must be the approved-client one.

    **The body is read only after all of that**, so a malformed body is never a
    second way to tell a refused caller from an administrator. A 400, a 403 for
    scope, or a 503 (write path disabled, or the project registry unreadable)
    is seen only by a verified administrator naming a real person.

    **Each product named must be inside the caller's own product scope**
    (`identity.in_scope`), or the whole request is refused before anything is
    written. A scoped administrator credential could otherwise grant a product
    it cannot see, which the read route would then hide from it.

    **A grant or revoke that touches a default project — one this person
    created, by the registry's own record — reads and writes
    `person_product_removals` rather than `person_products`**: see
    `store.change_person_products`. This route resolves the default with
    `projects.slugs_created_by()` and fails CLOSED, as `GET
    /people/{person_id}/projects` does, when the registry cannot be read —
    an unavailable registry must not read as "created nothing" and silently
    treat every default project as an ordinary grant.

    Grants and revocations are recorded in `person_product_changes` with the
    administrator and the time (`0652cc6b2cee`); a request that changes nothing
    records nothing.

    **AN ACCEPTED TIMING CHANNEL, RECORDED RATHER THAN CLOSED**, on the same
    terms as `blobs.py` and `adjacency.py` record theirs. `and` short-circuits,
    so a caller who is not an administrator costs one query and an administrator
    naming a person who does not exist costs two, though the two responses are
    byte-identical. Do not equalise it, and do not add a dummy query: the only
    caller who can reach either branch already holds an approved-client
    credential carrying `person.write`, `ident.actor` is fixed by that
    credential so the only name they can probe is their own, and an
    administrator reads the same answer straight off the status, since an empty
    body returns 400 to them and 404 to everyone else **when the person named
    exists**. Equalising would spend a query on every refusal to hide something
    the same caller is told outright (security-reviewer, PR #135).

    **That qualifier is load-bearing, so do not test this with a made-up person
    id.** Naming a person who does not exist returns the same 404 to an
    administrator and a non-administrator alike — which is the byte-identity
    this route is built for — so a reader probing with an invented id sees no
    difference, concludes the paragraph above is wrong, and re-opens a question
    that is settled. Name a person who exists, and the 400/404 split appears.
    What the channel discloses either way is only the caller's own administrator
    status: an administrator costs two queries whether or not the person exists,
    so it says nothing about anyone else.
    """
    ident, err = _identify(request)
    refused = refusals.not_found()
    if err or ident.kind != "approved_client" or not identity.authorized(
            ident, "person.write"):
        return refused
    conn = store.connect()
    try:
        allowed = (store.is_administrator(conn, ident.actor)
                   and store.person_exists(conn, person_id))
    finally:
        conn.close()
    if not allowed:
        return refused
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    grant, bad = _project_names(payload, "grant")
    if bad:
        return bad
    revoke, bad = _project_names(payload, "revoke")
    if bad:
        return bad
    if not grant and not revoke:
        return refusals.refuse(400, "name at least one project to grant or revoke")
    both = sorted(set(grant) & set(revoke))
    if both:
        return refusals.refuse(
            400, "a project may not be both granted and revoked in one request",
            projects=both)
    outside = [p for p in sorted({*grant, *revoke})
               if not identity.in_scope(ident, p)]
    if outside:
        return refusals.refuse(
            403, "this credential's product scope does not cover every "
                 "project named", projects=outside)
    default = projects.slugs_created_by(person_id)
    if default is None:
        return refusals.refuse(503, "the project registry is unavailable")
    try:
        changed = await run_in_threadpool(
            store.change_person_products, ident.actor, person_id, grant, revoke,
            default)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    if changed is None:
        # The administrator or the person went away between the read above and
        # the write lock, so this is the refusal the read would have given.
        return refused
    return {"person_id": person_id, "granted": changed["granted"],
            "revoked": changed["revoked"], "changed_by": ident.actor}


@app.put("/people/{person_id}/email")
async def set_person_email(person_id: str, request: Request):
    """Set or clear `person_id`'s sign-in email — the fifth slice of person
    identity in the Library (issue_01M4QQZ7VTANJ5ZGF630DCB0Y7). Body:
    `{"email": "<address>"}` sets it, `{"email": null}` clears it. Read it
    back with `POST /people/by-email/projects`.

    **GATED EXACTLY AS `POST /people/{person_id}/projects` IS, for the same
    reason**: `person.write` (approved-client credentials only) and a row in
    `administrators` for `ident.actor`. `acting_human` is never read here,
    and every refusal before the body is read is the same `refusals.not_found()`
    — no credential, an unresolvable one, a non-administrator, and an
    administrator naming a person who does not exist are byte-identical, for
    the reasons that route's own docstring gives in full.

    **The body is read only after all of that**, so a 400 for a malformed
    email or a 409 for one another person holds is seen only by a verified
    administrator naming a real person. `_parse_email` does the same three
    checks — one `"@"`, no whitespace, a bounded length — whether this call
    is setting an email or `POST /people/by-email/projects` is reading by
    one, and stores it trimmed and lower-cased, so the unique index in
    `ad6a0a903c09` compares the same value every write produces.

    **A 409 on a held email never names the other person.** `email` is
    looked up and compared to `person_id` inside `store.set_person_email`'s
    own write lock, and the refusal says only that the address is taken —
    the same discipline `change_person_products`'s own docstring explains
    for a lease conflict, applied here because the other holder's identity
    is not this caller's to learn.

    **Every set or clear is recorded with its administrator and time**, in
    `person_email_changes`, the way a change to the project set is recorded
    in `person_product_changes` (`0652cc6b2cee`).
    """
    ident, err = _identify(request)
    refused = refusals.not_found()
    if err or ident.kind != "approved_client" or not identity.authorized(
            ident, "person.write"):
        return refused
    conn = store.connect()
    try:
        allowed = (store.is_administrator(conn, ident.actor)
                   and store.person_exists(conn, person_id))
    finally:
        conn.close()
    if not allowed:
        return refused
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict) or "email" not in payload:
        return refusals.refuse(
            400, 'body must be a JSON object with an "email" field '
                 '(a string, or null to clear)')
    raw = payload["email"]
    if raw is None:
        email = None
    else:
        email, bad = _parse_email(raw)
        if bad:
            return bad
    try:
        result = await run_in_threadpool(
            store.set_person_email, ident.actor, person_id, email)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    if result is None:
        # The administrator or the person went away between the read above
        # and the write lock, so this is the refusal the read would have
        # given.
        return refused
    if result is store.EMAIL_TAKEN:
        return refusals.refuse(409, "this email is already in use")
    return {"person_id": person_id, "email": result["email"],
            "changed_by": ident.actor}


@app.get("/people/{person_id}/projects/report")
async def person_projects_report(person_id: str, request: Request):
    """The administrator's view of `person_id`'s projects — the fourth slice
    of person identity in the Library (issue_01M2RSFKJBEHD5Y76X0B6XAYZG): the
    effective set, the default it was derived from, and each grant or
    removal that differs from the default.

    **Gated exactly as `POST /people/{person_id}/projects` is, and for the
    same reason**: `person.write` (not `person.read` — this is the
    administrator's own view, not the three-caller read route above) plus a
    row in `administrators` for `ident.actor`. A non-administrator's refusal
    is the same `refusals.not_found()` a missing person gets, byte for byte,
    so this route discloses nothing a probing caller could not already get
    from the write route's own refusal.

    **Fails CLOSED on an unreadable registry**, after the administrator gate
    rather than before it — a malformed-body-shaped failure is seen only by a
    verified administrator naming a real person, the same ordering the write
    route uses for its own 503s.

    **`default`, `projects` and `deviations` are each bounded by the caller's
    own scope**: the two tables through `_scope_clause`, the registry-derived
    default through `identity.in_scope`, for `GET /people/{person_id}/projects`'s
    own reason — a scoped administrator must not see, or learn the shape of,
    a project outside what its credential already reaches.

    **A deviation names the project, which action it is (`"grant"` — held
    beyond the default, or `"removal"` — a default no longer held), who made
    it and when.** An explicit grant that happens to equal a default project
    is not a deviation: nothing differs from the default, so nothing is
    listed — `store.change_person_products` never writes such a grant row in
    the first place, but a grant made by hand before this slice, or before a
    registry change made the project this person's default, is read the same
    way rather than flagged.
    """
    ident, err = _identify(request)
    refused = refusals.not_found()
    if err or ident.kind != "approved_client" or not identity.authorized(
            ident, "person.write"):
        return refused
    conn = store.connect()
    try:
        allowed = (store.is_administrator(conn, ident.actor)
                   and store.person_exists(conn, person_id))
        if not allowed:
            return refused
        # After the administrator check, so the 400 tells nobody the 404
        # hides that this route exists (issue_01M1MTCTH3PW0DWDEXAZA5KFET).
        if (err := _reject_unknown_params(request, set())):
            return err
        default_all = projects.slugs_created_by(person_id)
        if default_all is None:
            return refusals.refuse(503, "the project registry is unavailable")
        default_set = {p for p in default_all if identity.in_scope(ident, p)}
        scope, scope_params = _scope_clause(ident)
        held_rows = conn.execute(
            f"SELECT product, granted_by, granted_at FROM person_products "
            f"WHERE person_id = ?{scope} ORDER BY product",
            [person_id, *scope_params]).fetchall()
        # See `GET /people/{person_id}/projects` for why this is skipped
        # when `default_set` is empty.
        removed_rows = conn.execute(
            f"SELECT product, removed_by, removed_at FROM "
            f"person_product_removals WHERE person_id = ?{scope} "
            f"ORDER BY product",
            [person_id, *scope_params]).fetchall() if default_set else []
    finally:
        conn.close()
    held = {r["product"]: r for r in held_rows}
    removed = {r["product"]: r for r in removed_rows}
    effective = sorted((default_set - set(removed)) | set(held))
    deviations = sorted((
        [{"project": p, "action": "grant",
          "by": r["granted_by"], "at": r["granted_at"]}
         for p, r in held.items() if p not in default_set]
        + [{"project": p, "action": "removal",
            "by": r["removed_by"], "at": r["removed_at"]}
           for p, r in removed.items() if p in default_set]
    ), key=lambda d: d["project"])
    return {"person_id": person_id, "projects": effective, "count": len(effective),
            "default": sorted(default_set), "deviations": deviations}


# --- signal acknowledgment (ADR-0038) ------------------------------------------
#
# Ported from `convivy-orient/signal_api.py:397` (`ack_signal`) and `:430`
# (`unack_signal`) — a data migration, not a redesign. **HUMAN-RESERVED**: Orient
# gates these on `kind == "human"`; the Library resolves no token to `human`, so
# the credential that reaches them is `approved_client`, and the human travels as
# `acting_human` on the body. `identity.attested_human` extracts and bounds it,
# and `signal_acks.identity` records THAT, never the calling client's own name
# (ADR-0038's whole point: the Library records the human who acted).
#
# **`signal.ack` is this pair's own capability**, distinct from every other
# human-reserved one `approved_client` holds (`policy.write` is the live
# example) — separate capabilities rather than one shared grant, so revoking
# a client's ability to do one need not also revoke another.
#
# **NOT DURABLY RECORDED: which client attested.** `signal_acks` carries no
# column for it — the table is still Orient's until the vault cutover, and the
# column lands with the Library's own alembic baseline rather than a migration
# added here. The attesting client is echoed in the response (`attested_by`) so
# a caller can see it in the meantime.


def _ack_visible(conn, ident: identity.Identity, signal_id: str) -> bool:
    """Same visibility rule `GET /signals/{id}` gives — out-of-scope, missing,
    and customer-class-without-the-cap are all indistinguishable — folded into
    one query through the customer wall rather than a second lookup, which is
    the join back to `signals.privacy_class` this route needs and a check on
    `signal_acks` alone could not give it.

    **Also applies `_private_handoff`** (security-reviewer, PR #185 round 1):
    this gates `GET /signals/acks`, `POST /signals/{id}/ack` and `DELETE
    /signals/{id}/ack`, and without it an identity in scope for a private
    product — including one scoped to that product itself — could ack a
    private handoff, confirm it exists by the ack succeeding, and read back
    whether it is acked, the identical oracle the write-side gate exists to
    prevent, reopened on a sibling route. Applies to a cold handoff exactly
    as to a hot one — see the hot/cold note just below.

    **Hot first, then cold on a miss** (archives-cold-tier.md §1 row 8).
    `signal_acks` itself stays in `main` regardless of which tier the
    signal it names is in. Unlike `_readable_signal`, a cold-attach failure
    here answers "not visible" rather than 503 — this helper has no
    response of its own to shape, and every caller already treats "not
    visible" as the safe, narrower answer for every OTHER reason a signal
    might not qualify.
    """
    wall, wall_params = _customer_wall(ident)
    row = conn.execute(
        f"SELECT product, kind FROM signals WHERE id = ?{wall}",
        [signal_id, *wall_params]).fetchone()
    if row is None and store.attach_cold_or_reason(conn, readonly=True) is None:
        row = conn.execute(
            f"SELECT product, kind FROM cold.signals WHERE id = ?{wall}",
            [signal_id, *wall_params]).fetchone()
    if row is None or not identity.in_scope(ident, row["product"]):
        return False
    if _is_handoff(row["kind"]) and _private_handoff(row["product"]):
        return False
    return True


async def _ack_request(request: Request):
    """The capability check, the body, and the attested human — shared by ack
    and unack, since both refuse and attest identically and differ only in what
    they do to the row once past this point.

    Returns `(ident, human, err)`; `err` is a response to return as-is when set,
    and `ident`/`human` are meaningless then.
    """
    ident, err = _identify(request)
    if err:
        return None, None, err
    if not identity.authorized(ident, "signal.ack"):
        return None, None, refusals.forbidden("signal.ack")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return None, None, refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return None, None, refusals.refuse(400, "body must be a JSON object")
    human, attest_err = identity.attested_human(ident, payload)
    if attest_err:
        return None, None, refusals.refuse(403, attest_err["error"])
    return ident, human, None


@app.post("/signals/{signal_id}/ack")
async def ack_signal(signal_id: str, request: Request):
    """Acknowledge a signal as the attested human. Idempotent.

    **No CSRF check.** Orient's version checks `same_origin` because its caller
    is a browser holding a session cookie; this route's caller is a service
    presenting a bearer token, so there is no origin for a forged cross-site
    request to spoof.
    """
    ident, human, err = await _ack_request(request)
    if err:
        return err
    conn = store.connect()
    try:
        visible = _ack_visible(conn, ident, signal_id)
    finally:
        conn.close()
    if not visible:
        return refusals.not_found()
    try:
        await run_in_threadpool(store.ack_signal, signal_id, human)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    return {"signal_id": signal_id, "acknowledged": True,
            "acting_human": human, "attested_by": ident.actor}


@app.delete("/signals/{signal_id}/ack")
async def unack_signal(signal_id: str, request: Request):
    """Withdraw this human's acknowledgment. Idempotent — a misfired tap must be
    reversible, or the safe move becomes never acking at all."""
    ident, human, err = await _ack_request(request)
    if err:
        return err
    conn = store.connect()
    try:
        visible = _ack_visible(conn, ident, signal_id)
    finally:
        conn.close()
    if not visible:
        return refusals.not_found()
    try:
        await run_in_threadpool(store.unack_signal, signal_id, human)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    return {"signal_id": signal_id, "acknowledged": False,
            "acting_human": human, "attested_by": ident.actor}


# --- tool policy (ADR-0048) --------------------------------------------------
#
# **THE READ SIDE IS OPEN, THE WRITE SIDE ISN'T, and that asymmetry is the
# whole mechanism.** `policy.read` sits on `agent` and `system` — every
# dispatched caller and everything that individuates one needs to ask "may
# this combination use this target" — where `policy.write` sits nowhere but
# `approved_client`, matching `signal.ack` (`identity.py`).
# The write route also refuses unconditionally on `ident.kind !=
# "approved_client"`, belt-and-suspenders on top of the capability check, the
# same shape `_ack_request` already uses.


@app.get("/policy/version")
async def policy_version(request: Request):
    """The cheap check ADR-0048 asks for: a number that changes whenever an
    explicit policy row is written, so a cache can tell "nothing changed" from
    "refresh" without paying for the refresh to find out. A change to the
    connector or built-in classes ships as code and does not move it
    (`store.policy_version`)."""
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "policy.read"):
        return refusals.forbidden("policy.read")
    if (err := _reject_unknown_params(request, set())):
        return err
    version = await run_in_threadpool(store.policy_version)
    return {"version": version}


@app.get("/policy/resolve")
async def resolve_policy(request: Request):
    """The policy for one (agent, crew, model) combination: every explicit
    override on file, plus the two classes that supply a default where no
    override exists.

    **Not a single yes/no, because the caller needs an answer for more than
    one target per dispatch.** `explicit` is a `{target: decision}` map,
    `connector_servers` and `exfiltration_builtins` are the two classes ADR-0048
    defines, and `default` names what everything else resolves to. A caller
    computes one target's answer as `explicit.get(target)`, falling back to
    `"deny"` when `target` is in either class and to `default` otherwise — one
    lookup here standing in for however many targets the caller needs to check.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "policy.read"):
        return refusals.forbidden("policy.read")
    if (err := _reject_unknown_params(
            request, {"agent", "crew", "model"})):
        return err
    # **STRIPPED BEFORE THE LOOKUP, not merely before the required-field
    # check** (reviewer, PR #41). This is an exact-match table by design (no
    # wildcards, per the ADR), so two spellings of "the same combination" that
    # differ only in incidental whitespace would silently see different rows
    # — a write's `"scout "` and a read's `"scout"` never meeting is a grant
    # that looks like it worked and does nothing. Matches the convention
    # every other identifier-shaped field in this file already follows
    # (`dec_id.strip()`, `run_id.strip()`).
    agent = (request.query_params.get("agent") or "").strip() or None
    crew = (request.query_params.get("crew") or "").strip() or None
    model = (request.query_params.get("model") or "").strip() or None
    for name, value in (("agent", agent), ("crew", crew), ("model", model)):
        if not value:
            return refusals.refuse(400, f"{name} is required")
        if len(value) > store.MAX_POLICY_FIELD:
            return refusals.refuse(
                400, f"{name} is longer than {store.MAX_POLICY_FIELD} characters")
    explicit, version = await run_in_threadpool(
        lambda: (store.resolve_policy(agent, crew, model), store.policy_version()))
    return {
        "agent": agent, "crew": crew, "model": model,
        "explicit": explicit,
        "connector_servers": sorted(store.POLICY_CONNECTOR_SERVERS),
        "exfiltration_builtins": sorted(store.POLICY_EXFIL_BUILTINS),
        "default": store.POLICY_ALLOW,
        "version": version,
    }


def _policy_key(payload: dict) -> tuple[dict | None, JSONResponse | None]:
    """The (agent, crew, model, target) combination a policy write or removal
    names, each field stripped and bounded, or a refusal. Shared by `PUT` and
    `DELETE /policy` so the two cannot disagree about which row a body names.

    **STRIPPED BEFORE VALIDATION AND BEFORE STORAGE** (reviewer, PR #41),
    matching the convention this file already follows for every other
    identifier-shaped field meant for exact matching (`acting_human` via
    `identity.attested_human`, `dec_id.strip()`, `run_id.strip()`
    elsewhere). This table has no wildcard matching by design, so a value
    stored with incidental whitespace is a row no real lookup ever finds
    again — a write that returns 200 and then does nothing.

    **THE TYPE CHECK GATES REJECTION; STRIPPING IS A SEPARATE STEP**
    (reviewer, PR #41, round 2). The first pass at this fix let `isinstance`
    decide only whether to strip, which is not the same check as deciding
    whether to refuse: a truthy non-string reached `len()` uncaught (a 500 on
    `{"agent": 12345}`) or, for a truthy container, passed straight through
    to `store.clip`, whose documented behaviour turns a non-string into an
    empty string — a write returning 200 for a row no real caller could ever
    address, the identical failure class this whole fix exists to close."""
    stripped = {}
    for field in ("agent", "crew", "model", "target"):
        value = payload.get(field)
        if not isinstance(value, str):
            return None, refusals.refuse(400, f"{field} is required")
        value = value.strip()
        if not value:
            return None, refusals.refuse(400, f"{field} is required")
        if len(value) > store.MAX_POLICY_FIELD:
            return None, refusals.refuse(
                400, f"{field} is longer than {store.MAX_POLICY_FIELD} "
                     f"characters")
        stripped[field] = value
    return stripped, None


@app.put("/policy")
async def write_policy(request: Request):
    """Set the decision for one (agent, crew, model, target) combination.
    Orient-only-write (ADR-0048).

    **Three things together, none sufficient alone**, matching the ADR's own
    description of the mechanism: `policy.write` is structurally absent from
    `agent` and `system` (`identity.py`); this route refuses any identity that
    is not `approved_client` outright, with no exception, matching Orient's
    `push_api.py`/`blog_api.py` pattern; and the human recorded as having made
    the change is `attested_human`'s resolved value, never a client-claimed
    field, which is what stops the check being evaluated against what the
    request says rather than what it actually resolves to.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "policy.write"):
        return refusals.forbidden("policy.write")
    if ident.kind != "approved_client":
        # UNREACHABLE TODAY — `policy.write` is absent from every other role,
        # so the capability check above already refused it. Kept anyway,
        # matching `attested_human`'s own reasoning: a capability table that
        # is correctly configured now is not a guarantee it stays that way,
        # and this is the check that holds even if it does not.
        return refusals.forbidden("policy.write")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    human, attest_err = identity.attested_human(ident, payload)
    if attest_err:
        return refusals.refuse(403, attest_err["error"])
    stripped, err = _policy_key(payload)
    if err:
        return err
    decision = payload.get("decision")
    # **`isinstance` BEFORE THE SET TEST, because a dict or a list is
    # unhashable and `x not in frozenset(...)` RAISES on one** rather than
    # answering False (reviewer, PR #104). Every other field here goes through
    # a type guard first; `decision` skipped it, so `{"decision": []}` reached
    # the client as an unhandled 500 instead of the 400 this line exists to
    # give. Confirmed in a shell rather than reasoned about.
    if not isinstance(decision, str) or decision not in store.POLICY_DECISIONS:
        return refusals.refuse(
            400, "decision must be one of the classes this vault defines",
            hint="an unrecognised decision is refused rather than guessed at",
            decision=decision, supported=sorted(store.POLICY_DECISIONS))
    row = {
        "agent": store.clip(stripped["agent"], store.MAX_POLICY_FIELD),
        "crew": store.clip(stripped["crew"], store.MAX_POLICY_FIELD),
        "model": store.clip(stripped["model"], store.MAX_POLICY_FIELD),
        "target": store.clip(stripped["target"], store.MAX_POLICY_FIELD),
        "decision": decision,
        # RECORDED AS THE ATTESTED HUMAN, NEVER `ident.actor` — the same
        # boundary `_ack_request` draws, and for the same reason: falling back
        # to the client's own identity here would record Orient as having
        # made the change rather than the person who did.
        "actor": human,
    }
    try:
        stored = await run_in_threadpool(store.upsert_policy, row)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    version = await run_in_threadpool(store.policy_version)
    return {
        "agent": stored["agent"], "crew": stored["crew"],
        "model": stored["model"], "target": stored["target"],
        "decision": stored["decision"], "acting_human": human,
        "attested_by": ident.actor, "version": version,
    }


@app.delete("/policy")
async def remove_policy(request: Request):
    """Remove the explicit row for one (agent, crew, model, target)
    combination, so it falls back to its class default (CQ·134). The tool-
    policy specification says the operator edits the policy by "adding a
    denial, removing one, or changing its scope", and before this route a
    row could be set to `allow` but never returned to "no entry on file".

    **The same gate as `PUT /policy`**: `policy.write`, the unconditional
    `approved_client` refusal, and `attested_human` as the recorded actor.
    Removing a denial is a permission change, so it takes the identical gate.
    The body names the row exactly as a write does, through `_policy_key`.

    **A black-box or private crew's row is refused, 409.** `crew_registry.
    withheld` finds such a crew's agents through these rows to keep their
    names off the roster, so removing one would publish a name whose prefix
    does not give it away. Those rows change through the crew's registration.
    An unreadable project registry refuses 503, closed.

    No row on file answers 404 and writes no event."""
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "policy.write"):
        return refusals.forbidden("policy.write")
    if ident.kind != "approved_client":
        return refusals.forbidden("policy.write")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    human, attest_err = identity.attested_human(ident, payload)
    if attest_err:
        return refusals.refuse(403, attest_err["error"])
    key, err = _policy_key(payload)
    if err:
        return err

    def _remove():
        if key["crew"].casefold() in crew_registry.withholding_crews():
            return None, refusals.refuse(
                409, "this row is a withheld crew's declared permission",
                hint="a black-box or private crew's rows change through its "
                     "registration, because the roster finds its agents "
                     "through them")
        removed = store.remove_policy(key, human)
        if removed is None:
            return None, refusals.refuse(
                404, "no explicit row is on file for this combination")
        return (removed, store.policy_version()), None

    try:
        result, refusal = await run_in_threadpool(_remove)
    except crew_registry.RegistryUnreadable as e:
        return refusals.refuse(
            503, str(e),
            hint="retry once the registry at the configured path is "
                 "readable again")
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    if refusal:
        return refusal
    removed, version = result
    return {
        "agent": removed["agent"], "crew": removed["crew"],
        "model": removed["model"], "target": removed["target"],
        "removed_decision": removed["decision"], "acting_human": human,
        "attested_by": ident.actor, "version": version,
    }


@app.get("/policy")
async def list_policy(request: Request):
    """Every explicit policy row, narrowed by exact match on any of `agent`,
    `crew`, `model` and `target`, paged (CQ·134). `GET /policy/resolve`
    answers for one known (agent, crew, model) triple, so before this route
    nothing could show what the table had accumulated.

    **Gated like a write, to `policy.write` and `approved_client`.** The rows
    name every agent a black-box crew registered, which `crew_registry.
    withheld` keeps off the roster; serving them to every `policy.read`
    holder would publish those names. Administering the table is the
    operator's act, through the client that writes it."""
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "policy.write"):
        return refusals.forbidden("policy.write")
    if ident.kind != "approved_client":
        return refusals.forbidden("policy.write")
    if (err := _reject_unknown_params(
            request, {"agent", "crew", "model", "target", "limit", "offset"})):
        return err
    limit, offset, err = _page(request)
    if err:
        return err
    filters = {}
    for field in ("agent", "crew", "model", "target"):
        value = (request.query_params.get(field) or "").strip()
        if not value:
            continue
        if len(value) > store.MAX_POLICY_FIELD:
            return refusals.refuse(
                400, f"{field} is longer than {store.MAX_POLICY_FIELD} characters")
        filters[field] = value
    (rows, total), version = await run_in_threadpool(
        lambda: (store.list_policy(filters, limit, offset), store.policy_version()))
    return {"rows": rows, "count": len(rows), "total": total, "offset": offset,
            "truncated": offset + len(rows) < total, "version": version}


# --- black-box project registration (issue_01M19QQNTWD4RD9MSZZRKHYM7N) ------
#
# **THE DECLARED TOOL PERMISSIONS ARE `tool_policy` ROWS, WRITTEN THROUGH
# `store.upsert_policy` — THE SAME FUNCTION `PUT /policy` USES, NOT A SECOND
# STORE FOR THE SAME FACT.** A black-box crew declaring "this agent may use
# this target" is granting tool access, which is exactly what `policy.write`
# already exists to gate. `GET /policy/resolve` answers for a registered
# black box exactly as it does for anything else — there is no special case
# and no parallel resolve path. The one new table, `black_box_
# registrations`, holds only the two fields `tool_policy` has no column for:
# the model and gateway binding the crew itself declares.
#
# **`policy.write` IS THE CAPABILITY, NOT A NEW ONE.** This route matches
# `write_policy`'s exact shape: the capability check, then the unconditional
# `ident.kind != "approved_client"` refusal, then `identity.attested_human`
# for `acting_human`. A black box declaring `decision: allow` is granting
# tool access, the same act `write_policy` gates, so it takes the same
# capability rather than a route-specific one — see `identity.py`'s comments
# beside `policy.write` for why that capability lives nowhere but
# `approved_client`.
#
# **THIS ROUTE DOES NOT ADJUDICATE WHETHER `crew` IS REALLY A BLACK BOX, AND
# THAT IS DELIBERATE.** `projects.private_repos()` is ADR-0043's live
# registry of which crews are marked private, and ADR-0043 is explicit that
# it is narrower than the true boundary: Ask is a deliberately UNREGISTERED
# black box with no registry entry at all. A check written against the
# registry would refuse the registration of a crew that IS a black box. The
# crew declares; the Library audits the SHAPE of the declaration and records
# it — membership is ADR-0043's to name, not this route's to verify. **Do
# not add a `projects.private_repos()` check here.** The next reader's
# instinct will be to add one, and it would narrow this route to a boundary
# that is not the true one.
#
# **`create_signal` (`POST /signals`) DOES check `private_repos()` and
# `private_crew_names()`, and that is the other direction rather than a
# contradiction of this one.** This
# route registers a crew's declared SHAPE; `create_signal` is a content
# write, exactly what ADR-0043 walls off — see its own comment for why the
# same check that must not sit here belongs there.
#
# **"SEES NO CONTENT" IS THE AUDIT, AND IT IS THIS ROUTE'S SECURITY
# PROPERTY.** Every accepted value is an identifier bounded by
# `store.MAX_IDENT`: crew, agent, model, target, gateway, decision. There is
# no free-text field, no path, no body, no description, no note — a field
# that could carry prose is a field through which a black box's content
# reaches the Library. An unrecognised key is refused by name rather than
# ignored, matching `crew_registry._validate_include_block`'s precedent: an
# ignored key is how content arrives unnoticed.

_BLACK_BOX_TOP_KEYS = frozenset(
    {"acting_human", "crew", "model", "gateway", "permissions"})
_BLACK_BOX_PERMISSION_KEYS = frozenset({"agent", "model", "target", "decision"})


def _clean_black_box_ident(payload: dict, field: str) -> tuple[str | None, str | None]:
    """A required identifier-shaped string: present, a string, stripped,
    non-empty, and bounded by `store.MAX_IDENT`. Returns `(value, error)` —
    `error` is a plain message naming `field`, left for the caller to wrap
    in a refusal (and, for a `permissions[i]` entry, to prefix), rather than
    a `JSONResponse` this function would otherwise have to let the caller
    inspect to reuse."""
    value = payload.get(field)
    if not isinstance(value, str):
        return None, f"{field} is required"
    value = value.strip()
    if not value:
        return None, f"{field} is required"
    if len(value) > store.MAX_IDENT:
        return None, f"{field} is longer than {store.MAX_IDENT} characters"
    return value, None


@app.post("/black-box/registrations")
async def register_black_box(request: Request):
    """Register a crew as a black box: confirm it belongs to a black-box
    project and declare the tool permissions, model and gateway bindings it
    wants (issue_01M19QQNTWD4RD9MSZZRKHYM7N). Orient-only-write, matching
    `write_policy` — the declared permissions are grants of tool access, the
    same act that route gates.

    See the module-level comment above for why this route neither checks
    `projects.private_repos()` nor accepts any field that could carry a
    black box's content rather than the shape of its declaration.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "policy.write"):
        return refusals.forbidden("policy.write")
    if ident.kind != "approved_client":
        # UNREACHABLE TODAY — `policy.write` is absent from every other
        # role, so the capability check above already refused it. Kept
        # anyway, matching `write_policy`'s own reasoning: a capability
        # table that is correctly configured now is not a guarantee it
        # stays that way.
        return refusals.forbidden("policy.write")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")

    # **THE CLOSED SET, CHECKED BEFORE ANYTHING ELSE READS THE BODY.** A
    # black box's content reaches the Library through exactly one door: a
    # field this route did not expect and forwarded anyway. Refusing by
    # name, rather than silently ignoring, is what makes an ignored key
    # impossible rather than merely unlikely.
    unknown = set(payload.keys()) - _BLACK_BOX_TOP_KEYS
    if unknown:
        return refusals.refuse(
            400, f"unrecognised field(s) {sorted(unknown)}; only "
                 f"{sorted(_BLACK_BOX_TOP_KEYS)} are accepted — a black-box "
                 f"crew declares shape, not content")

    human, attest_err = identity.attested_human(ident, payload)
    if attest_err:
        return refusals.refuse(403, attest_err["error"])

    crew, e = _clean_black_box_ident(payload, "crew")
    if e:
        return refusals.refuse(400, e)
    model, e = _clean_black_box_ident(payload, "model")
    if e:
        return refusals.refuse(400, e)
    gateway, e = _clean_black_box_ident(payload, "gateway")
    if e:
        return refusals.refuse(400, e)

    permissions = payload.get("permissions", [])
    if not isinstance(permissions, list):
        return refusals.refuse(400, "permissions must be a list")
    if len(permissions) > store.MAX_BLACK_BOX_PERMISSIONS:
        return refusals.refuse(
            400, f"permissions is longer than "
                 f"{store.MAX_BLACK_BOX_PERMISSIONS} entries")

    cleaned_permissions = []
    for i, block in enumerate(permissions):
        if not isinstance(block, dict):
            return refusals.refuse(400, f"permissions[{i}] must be an object")
        unknown = set(block.keys()) - _BLACK_BOX_PERMISSION_KEYS
        if unknown:
            return refusals.refuse(
                400, f"permissions[{i}] has unrecognised field(s) "
                     f"{sorted(unknown)}; only "
                     f"{sorted(_BLACK_BOX_PERMISSION_KEYS)} are accepted — a "
                     f"black-box crew declares shape, not content")
        p_agent, e = _clean_black_box_ident(block, "agent")
        if e:
            return refusals.refuse(400, f"permissions[{i}].{e}")
        p_model, e = _clean_black_box_ident(block, "model")
        if e:
            return refusals.refuse(400, f"permissions[{i}].{e}")
        p_target, e = _clean_black_box_ident(block, "target")
        if e:
            return refusals.refuse(400, f"permissions[{i}].{e}")
        decision = block.get("decision")
        # The same unhashable-value crash `write_policy` above carries, fixed
        # in both places rather than only the new one: a repository holding
        # the identical defect two hundred lines apart, with one side fixed,
        # reads as a deliberate difference.
        if not isinstance(decision, str) or decision not in store.POLICY_DECISIONS:
            return refusals.refuse(
                400, f"permissions[{i}].decision must be one of the classes "
                     f"this vault defines",
                hint="an unrecognised decision is refused rather than "
                     "guessed at",
                decision=decision, supported=sorted(store.POLICY_DECISIONS))
        cleaned_permissions.append({"agent": p_agent, "model": p_model,
                                    "target": p_target, "decision": decision})

    # **NOT ONE TRANSACTION ACROSS THE REGISTRATION AND ITS PERMISSIONS.**
    # Each is its own `store.upsert_policy`/`upsert_black_box_registration`
    # call and its own connection, the same shape `issue_machine_certificate`
    # and `crew_registry.profile_put` already accept for their own single
    # writes — every field here is validated above before any write starts,
    # and `WRITES_ENABLED` cannot flip mid-request, so the only way a later
    # call in this loop fails is the database itself failing, a class this
    # codebase does not currently guard against anywhere it writes more than
    # one row per request.
    try:
        stored = await run_in_threadpool(
            store.upsert_black_box_registration,
            {"crew": crew, "model": model, "gateway": gateway,
             "actor": human})
        for perm in cleaned_permissions:
            await run_in_threadpool(store.upsert_policy, {
                "agent": perm["agent"], "crew": crew, "model": perm["model"],
                "target": perm["target"], "decision": perm["decision"],
                "actor": human,
            })
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))

    return {
        "crew": stored["crew"], "model": stored["model"],
        "gateway": stored["gateway"],
        "permissions_count": len(cleaned_permissions),
        "acting_human": human, "attested_by": ident.actor,
    }


def _black_box_registration_dict(row: dict) -> dict:
    """The exact, closed set of fields a black-box registration read route
    returns: crew, model, gateway, and the audit trail of who wrote it and
    when, matching `register_black_box`'s "sees no content" property, since
    this table has no field that could carry more
    (issue_01M34SVJK9AVJX7VM8Y8A23HR7)."""
    return {
        "crew": row["crew"], "model": row["model"], "gateway": row["gateway"],
        "created_by": row["created_by"], "created_at": row["created_at"],
        "updated_by": row["updated_by"], "updated_at": row["updated_at"],
    }


@app.get("/black-box/registrations")
async def list_black_box_registrations(request: Request):
    """Every black-box crew's declared shape: the model and gateway it
    registered, and who wrote each value and when
    (issue_01M34SVJK9AVJX7VM8Y8A23HR7). `register_black_box` stores these
    two fields but returns them only in its own response to the call that
    wrote them; this route lets a later caller read them back.

    **This response omits the declared tool permissions.** They land in
    `tool_policy`, keyed by (agent, crew, model, target) rather than by
    crew alone. `store.resolve_policy`, the function `GET /policy/resolve`
    already uses, takes an exact (agent, crew, model) triple, so there is
    no cheap query from a bare crew name to every permission a crew
    declared that reuses that function rather than duplicating its join. A
    caller wanting a crew's grants asks `GET /policy/resolve` for each
    (agent, model) pair it cares about.

    **`approved_client` only, unlike `GET /policy/resolve`.** This is an
    audit surface for the small set of parties that need to see the whole
    registry — Deck, the Bosun, Orient, and a person's administrator
    credential — not the ordinary dispatched-work read `policy.read` grants
    every `agent` and `system` credential (security-reviewer, PR #174 round
    1). Listing every black-box crew in one call lets any dispatched agent
    enumerate the registry with no prior knowledge of a name, which is the
    exposure ADR-0043 asks a black-box crew's registration to minimise.
    Widening this to `agent`/`system` is a decision for a later change with
    a named consumer, not a default to fall back to here.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "policy.read"):
        return refusals.forbidden("policy.read")
    if ident.kind != "approved_client":
        return refusals.forbidden("policy.read")
    if (err := _reject_unknown_params(request, set())):
        return err
    rows = await run_in_threadpool(store.black_box_registrations)
    registrations = [_black_box_registration_dict(r) for r in rows]
    return {"registrations": registrations, "count": len(registrations)}


@app.get("/black-box/registrations/{crew:path}")
async def get_black_box_registration(crew: str, request: Request):
    """One black-box crew's declared shape, read back exactly as
    `register_black_box` stored it (issue_01M34SVJK9AVJX7VM8Y8A23HR7); 404
    when `crew` never registered. `list_black_box_registrations` above
    explains why the declared tool permissions live outside this response
    and where to read them, and why this route is `approved_client`-only.

    **`:path`, not the default segment converter** (reviewer, PR #174 round
    1). `_clean_black_box_ident` puts no character restriction on a
    registered `crew`, so a crew registered with a `/` in its name would
    404 on the default converter before the handler ever ran — the same
    fix `GET /reference/documents/{doc_id:path}` already applies because
    `doc_id` can contain slashes.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "policy.read"):
        return refusals.forbidden("policy.read")
    if ident.kind != "approved_client":
        return refusals.forbidden("policy.read")
    if (err := _reject_unknown_params(request, set())):
        return err
    crew = crew.strip()
    if not crew:
        return refusals.refuse(400, "crew is required")
    if len(crew) > store.MAX_POLICY_FIELD:
        return refusals.refuse(
            400, f"crew is longer than {store.MAX_POLICY_FIELD} characters")
    rows = await run_in_threadpool(store.black_box_registrations, crew)
    if not rows:
        return refusals.not_found()
    return _black_box_registration_dict(rows[0])


# --- machine certificate registry (platform/decisions/0066) -----------------
#
# **THE READ SIDE ALREADY EXISTS AND STAYS UNTOUCHED.** `machine_identity.
# MachineIdentityGate` reads this table on every mutual-TLS request; this is
# the write side, matching `write_policy`'s shape exactly: `machine.write` is
# structurally absent from `agent` and `system` (`identity.py`), the route
# refuses any identity that is not `approved_client` outright, and the human
# recorded as having issued or revoked the certificate is `attested_human`'s
# resolved value, never a client-claimed field.
#
# **The single-live-certificate invariant is `machine_registry.issue`'s job,
# not this route's.** It revokes whatever was active for a machine in the
# same transaction as the new row, so this route — and the local admin
# command, `enroll_machine.py` — cannot forget to do it by construction
# rather than by remembering to call revoke first.


async def _machine_write_request(request: Request):
    """The capability check and the attested human — shared by issue and
    revoke, the same split `_ack_request` uses for ack/unack.

    Returns `(ident, human, payload, err)`; `err` is a response to return as
    -is when set, and the other three are meaningless then.
    """
    ident, err = _identify(request)
    if err:
        return None, None, None, err
    if not identity.authorized(ident, "machine.write"):
        return None, None, None, refusals.forbidden("machine.write")
    if ident.kind != "approved_client":
        # UNREACHABLE TODAY — `machine.write` is absent from every other
        # role, so the capability check above already refused it. Kept
        # anyway, matching `write_policy`'s own reasoning: a capability
        # table that is correctly configured now is not a guarantee it
        # stays that way.
        return None, None, None, refusals.forbidden("machine.write")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return None, None, None, refusals.refuse(
            400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return None, None, None, refusals.refuse(
            400, "body must be a JSON object")
    human, attest_err = identity.attested_human(ident, payload)
    if attest_err:
        return None, None, None, refusals.refuse(403, attest_err["error"])
    return ident, human, payload, None


@app.post("/machines/{machine}/certificates")
async def issue_machine_certificate(machine: str, request: Request):
    """Register a newly-signed certificate for `machine`, revoking whatever
    certificate was previously active for it. Orient-only-write
    (platform/decisions/0066).

    **Takes the fingerprint and expiry the signing tool already printed,
    not a certificate file** — see `machine_registry.py`'s module docstring
    for why this route's body is metadata rather than a PEM upload.
    """
    ident, human, payload, err = await _machine_write_request(request)
    if err:
        return err

    machine = machine.strip()
    if not machine or len(machine) > store.MAX_IDENT:
        return refusals.refuse(
            400, f"machine must be 1-{store.MAX_IDENT} characters")
    fingerprint = payload.get("fingerprint_sha256")
    if not isinstance(fingerprint, str):
        return refusals.refuse(400, "fingerprint_sha256 is required")
    custody = payload.get("custody")
    not_after_raw = payload.get("not_after")
    if not isinstance(not_after_raw, str):
        return refusals.refuse(400, "not_after is required")
    try:
        not_after = _dt.datetime.fromisoformat(not_after_raw)
    except ValueError:
        return refusals.refuse(
            400, "not_after must be an ISO-8601 date or datetime")
    note = payload.get("note")
    if note is not None and not isinstance(note, str):
        return refusals.refuse(400, "note must be a string when present")

    try:
        row = await run_in_threadpool(
            machine_registry.issue, machine, fingerprint, not_after, custody,
            note=note)
    except machine_registry.InvalidCertificateRow as e:
        return refusals.refuse(400, str(e))
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    return {**row, "acting_human": human, "attested_by": ident.actor}


@app.post("/machines/{machine}/revoke")
async def revoke_machine_certificate(machine: str, request: Request):
    """Revoke the certificate currently active for `machine`. A machine with
    no active certificate answers 404, matching every other
    resolve-then-act route in this file rather than a bare no-op 200 that
    cannot distinguish "already revoked" from "never existed"."""
    ident, human, payload, err = await _machine_write_request(request)
    if err:
        return err
    note = payload.get("note")
    if note is not None and not isinstance(note, str):
        return refusals.refuse(400, "note must be a string when present")
    try:
        row = await run_in_threadpool(
            machine_registry.revoke, machine.strip(), note=note)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    if row is None:
        return refusals.not_found()
    return {**row, "acting_human": human, "attested_by": ident.actor}


@app.get("/machine-certificates/{fingerprint_sha256}")
async def read_machine_certificate(fingerprint_sha256: str, request: Request):
    """Whether a machine certificate is active now (platform/decisions/0066).
    The Bosun's own check against the one registry, in place of keeping a
    second list — one enrollment and one revocation then govern both
    servers. **Protocol version 1, and additive** — a Library that predates
    this route answers 404 to every caller, which a client must treat as not
    active.

    **200 whether or not the fingerprint is on file**, matching
    `read_signing_certificate`'s own reasoning: an unknown certificate
    answers `{"status": "unknown", "active": false}` rather than a 404, since
    a fingerprint the caller already holds reveals nothing worth protecting.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "machine.verify"):
        return refusals.forbidden("machine.verify")
    if (err := _reject_unknown_params(request, set())):
        return err
    try:
        result = await run_in_threadpool(
            machine_registry.status_for, fingerprint_sha256)
    except machine_registry.InvalidCertificateRow as e:
        return refusals.refuse(400, str(e))
    return result


# --- signing certificate registry (platform/contracts/library-protocol §2.2)
#
# **THE WRITE SIDE HOLDS ITS OWN CAPABILITY, `signing.write`, NOT A REUSE OF
# `machine.write`.** A round-1 security review of this PR named the risk in
# the reuse this comment used to describe: `machine.write` also decides
# which fingerprint the mTLS listener trusts as a machine, so a client
# granted either capability was granted both, whether or not that client
# should be trusted with the other act. `identity.py`'s comment beside
# `signing.write` carries the reasoning for the split; this file gates on it
# rather than repeating it.
#
# **THE READ SIDE IS ITS OWN CAPABILITY, `signing.verify`, granted to `agent`
# and `system` and so to `approved_client` — NOT human-reserved.** `kb apply`
# is the caller the contract names, and it authenticates as whichever of
# `agent` or `system` its own credential resolves to, neither of which may
# hold `machine.write` or `signing.write`.


async def _signing_write_request(request: Request):
    """The capability check and the attested human for the signing
    registry's two write routes — matching `_machine_write_request`'s shape,
    on `signing.write` rather than `machine.write` (see the comment above)."""
    ident, err = _identify(request)
    if err:
        return None, None, None, err
    if not identity.authorized(ident, "signing.write"):
        return None, None, None, refusals.forbidden("signing.write")
    if ident.kind != "approved_client":
        # UNREACHABLE TODAY, matching `_machine_write_request`'s own note:
        # kept anyway because a correctly configured table today is not a
        # guarantee it stays that way.
        return None, None, None, refusals.forbidden("signing.write")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return None, None, None, refusals.refuse(
            400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return None, None, None, refusals.refuse(
            400, "body must be a JSON object")
    human, attest_err = identity.attested_human(ident, payload)
    if attest_err:
        return None, None, None, refusals.refuse(403, attest_err["error"])
    return ident, human, payload, None


@app.post("/signing-certificates")
async def issue_signing_certificate(request: Request):
    """Register a newly-signed signing certificate.

    **Takes the fingerprint and expiry the signing tool already printed,
    not a certificate file** — see `signing_registry.py`'s module docstring
    for why this route's body is metadata rather than a PEM upload, matching
    `issue_machine_certificate`'s own reasoning.
    """
    ident, human, payload, err = await _signing_write_request(request)
    if err:
        return err

    fingerprint = payload.get("fingerprint_sha256")
    if not isinstance(fingerprint, str):
        return refusals.refuse(400, "fingerprint_sha256 is required")
    person = payload.get("person")
    if not isinstance(person, str):
        return refusals.refuse(400, "person is required")
    machine = payload.get("machine")
    if not isinstance(machine, str):
        return refusals.refuse(400, "machine is required")
    not_after_raw = payload.get("not_after")
    if not isinstance(not_after_raw, str):
        return refusals.refuse(400, "not_after is required")
    try:
        not_after = _dt.datetime.fromisoformat(not_after_raw)
    except ValueError:
        return refusals.refuse(
            400, "not_after must be an ISO-8601 date or datetime")
    note = payload.get("note")
    if note is not None and not isinstance(note, str):
        return refusals.refuse(400, "note must be a string when present")

    try:
        row = await run_in_threadpool(
            signing_registry.issue, fingerprint, person, machine, not_after,
            note=note)
    except signing_registry.InvalidCertificateRow as e:
        return refusals.refuse(400, str(e))
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    return {**row, "acting_human": human, "attested_by": ident.actor}


@app.post("/signing-certificates/{fingerprint}/revoke")
async def revoke_signing_certificate(fingerprint: str, request: Request):
    """Revoke the signing certificate named by `fingerprint`. No active row
    for it answers 404, matching `revoke_machine_certificate`'s own
    resolve-then-act shape."""
    ident, human, payload, err = await _signing_write_request(request)
    if err:
        return err
    note = payload.get("note")
    if note is not None and not isinstance(note, str):
        return refusals.refuse(400, "note must be a string when present")
    try:
        row = await run_in_threadpool(
            signing_registry.revoke, fingerprint.strip(), note=note)
    except signing_registry.InvalidCertificateRow as e:
        return refusals.refuse(400, str(e))
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    if row is None:
        return refusals.not_found()
    return {**row, "acting_human": human, "attested_by": ident.actor}


@app.get("/signing-certificates/{fingerprint}")
async def read_signing_certificate(fingerprint: str, request: Request):
    """Whether a signing certificate is active now (platform/contracts/
    library-protocol §2.2). **Protocol version 1, and additive** — a Library
    that predates this route answers 404 to every caller, which `kb apply`
    already treats as not active, so a signed save against an old Library
    refuses rather than being wrongly admitted.

    **200 whether or not the fingerprint is on file.** An unknown
    certificate answers `{"status": "unknown", "active": false}` rather than
    a 404: a fingerprint of a certificate the caller already holds reveals
    nothing worth protecting, and the contract's own wording is that a
    verifier needs one answer to test rather than two.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "signing.verify"):
        return refusals.forbidden("signing.verify")
    if (err := _reject_unknown_params(request, set())):
        return err
    try:
        result = await run_in_threadpool(
            signing_registry.status_for, fingerprint)
    except signing_registry.InvalidCertificateRow as e:
        return refusals.refuse(400, str(e))
    return result


@app.get("/ledgers")
async def list_ledgers(request: Request):
    """Every live ledger this identity may see, with today's total on each.

    **One route for the fleet-wide and the single-scope view** (the operator,
    2026-08-22), the same call `GET /reference/documents` and
    `GET /lease-refusals` already answer both ways. `?scope_id=` narrows.

    The total is TODAY's, in the caller's sense of today only if the caller
    shares this machine's timezone — so `day` is returned explicitly rather than
    left implicit. A client rendering "spent today" against a different local
    day would be reading a number that is right for a day it is not showing.

    **`total_usd` NEVER BLENDS `gateway` AND `subscription` SPEND
    (issue_01M35QG5NA13JE8FQVM9XET4HD), and it never did** — it is, and
    stays, `store.ledger_total`'s enforceable figure: every entry this route
    has ever reported before `lane` existed, plus a `gateway`-lane entry,
    minus nothing a `subscription`-lane entry ever added. `lanes` is the new
    field: `lanes.subscription` is the estimate the enforceable total always
    excluded, read fresh from the entries because nothing materializes it,
    and `lanes.gateway` is `total_usd` again, named for a client that wants
    the pair without reading two fields. The two are never summed anywhere
    in this response.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "ledger.read"):
        return refusals.forbidden("ledger.read")
    if (err := _reject_unknown_params(
            request, {"limit", "offset", "scope_id"})):
        return err
    limit, offset, err = _page(request)
    if err:
        return err
    day = local_day()
    where, params = "WHERE state = 'live'", []
    # SCOPED IN SQL, NOT AFTER THE FACT — `_scope_clause`'s own docstring names
    # the failure this avoids: a post-filter makes `total` count rows the caller
    # may not see, and makes a page come back shorter than the count a client
    # pages by. This table's product column is `scope_id`, so the clause is
    # written here rather than reused.
    #
    # **A WORKSTREAM LEDGER IS WITHHELD FROM A SCOPED CALLER RATHER THAN
    # GUESSED AT.** A workstream id is not a product, so no product filter can
    # judge it, and there are none yet. Showing them to every scoped caller
    # would leak what workstreams exist on the strength of a scoping rule nobody
    # has defined; withholding them costs nothing today and is the reversible
    # direction.
    if "*" not in ident.products:
        if not ident.products:
            where += " AND 1 = 0"
        else:
            names = sorted(ident.products)
            where += (f" AND scope_kind = 'project' AND scope_id IN "
                      f"({','.join('?' * len(names))})")
            params += names
    if (scope_id := request.query_params.get("scope_id")):
        # A FILTER MAY STILL NAME A REPOSITORY — every stored `scope_id` is
        # now the resolved project (see `_refuse_unbookable_product`), so a
        # caller filtering by the repository it knows gets resolved the same
        # way rather than matching nothing.
        scope_id = store.ledger_scope_name(scope_id)
        where += " AND scope_id = ?"
        params.append(scope_id)
    conn = store.connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) c FROM ledgers {where}", params).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM ledgers {where} ORDER BY scope_kind, scope_id "
            f"LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
        out = []
        for r in rows:
            row_total = store.ledger_total(conn, r["id"], day)
            subscription_total = store.ledger_lane_total(conn, r["id"], day,
                                                          "subscription")
            out.append({
                "id": r["id"], "scope_kind": r["scope_kind"],
                "scope_id": r["scope_id"], "day": day,
                "total_usd": row_total / 1_000_000,
                "lanes": {"gateway": row_total / 1_000_000,
                         "subscription": subscription_total / 1_000_000},
                "caps": {
                    "warn_usd": (r["warn_usd_micro"] / 1_000_000
                                if r["warn_usd_micro"] is not None else None),
                    "soft_cap_usd": (r["soft_cap_usd_micro"] / 1_000_000
                                    if r["soft_cap_usd_micro"] is not None else None),
                    "hard_cap_usd": (r["hard_cap_usd_micro"] / 1_000_000
                                    if r["hard_cap_usd_micro"] is not None else None),
                    "per_run_cap_usd": (r["per_run_cap_usd_micro"] / 1_000_000
                                       if r["per_run_cap_usd_micro"] is not None
                                       else None),
                },
                "state": store.ledger_state(r, row_total),
                "granted_by": r["granted_by"], "updated_at": r["updated_at"],
            })
    finally:
        conn.close()
    return {"ledgers": out, "day": day, "count": len(out), "total": total,
            "offset": offset, "truncated": offset + len(out) < total}


def _optional_amount(payload: dict, field: str):
    """A money amount from the wire, or `(None, None)` when the field is
    absent or explicitly `null` — the caller may leave a threshold unset.
    Present-and-invalid is still refused by `_amount`'s validator, the same
    one that catches `Infinity`/`NaN` and the oversized-report hole; a second,
    looser validator for the optional case would be a second place to fix that
    hole in.
    """
    if payload.get(field) is None:
        return None, None
    return _amount(payload, field)


async def _refuse_unbookable_product(product: str):
    """`(refusal, resolved_scope)` for a ledger product: a refusal when it is
    unregistered, private or a black box, with `resolved_scope` then `None`;
    otherwise `(None, resolved_scope)`, the registry's canonical project slug
    for `product`. Called by `POST /ledger-entries` and `PUT /ledgers`, the
    two routes that create a ledger row (issue_01M4PMVGGFVGDGBSJBTFA2T3RP,
    ADR-0043).

    Without it, `ensure_live_ledger` made a ledger for any string, so an
    `agent` credential scoped "*" could book spend to a black-box crew with
    its agent name in `agent`, and `GET /ledger-entries` and `GET /ledgers`
    served both back; `PUT /ledgers` could name one the same way. The
    private check is `create_signal`'s, for the same reasons its comment
    gives: the union of `private_repos()` and `private_crew_names()`, plus
    the black-box registrations, failing closed when the registry cannot be
    read.

    **An unregistered product is refused too, which `create_signal` does
    not do.** A ledger is a project's (`store.LEDGER_SCOPES`), so a name the
    registry does not know has no ledger, and ADR-0043's Ask is a black box
    with no registry entry at all, which only this refusal walls.

    **The scope is now stored and looked up as the resolved project slug,
    never the caller's own spelling.** Every one of the 12 live ledgers on
    2026-10-13 was keyed by a repository instead — `agent-platform` and
    `selector-backend` among them — because canonicalizing used to split a
    repository's spend into a second, uncapped ledger beside the project's.
    The migration folding every repository-keyed ledger into its project
    ships in the same change as this resolution, so the split this comment
    used to warn about is the thing being fixed rather than a risk still open.

    One 404 for all three, the same one the scope check returns, so neither
    route is an oracle for which crews are black boxes.
    """
    private = await run_in_threadpool(projects.private_repos)
    crew_names = await run_in_threadpool(projects.private_crew_names)
    if private is None or crew_names is None:
        return refusals.refuse(
            503, "the project registry could not be read, so a black-box "
                 "crew cannot be told apart from an ordinary one, and no "
                 "ledger is written",
            hint="retry once the registry at the configured path is "
                 "readable again"), None
    resolved = projects.resolve(product)
    if not resolved:
        return refusals.not_found(), None
    names = {product.casefold(), resolved.strip().casefold()}
    if names & (private | crew_names):
        return refusals.not_found(), None
    registered = await run_in_threadpool(store.black_box_registration_crews)
    if names & registered:
        return refusals.not_found(), None
    return None, resolved


@app.put("/ledgers")
async def grant_ledger(request: Request):
    """Set a scope's cap set — warn, soft cap, hard cap. Orient-only-write.

    **Three things together, none sufficient alone**, the shape ADR-0048's
    policy write already uses and for the same reason: `ledger.grant` is
    structurally absent from `agent` and `system` (`identity.py`); this route
    refuses any identity that is not `approved_client` outright; and the human
    recorded as having made the change is `attested_human`'s resolved value,
    never a client-claimed field.

    **Why the cap set is reserved when the spend is not.** Spending accumulates
    a ledger's total and this route decides what refuses it. If an agent could
    do the second, the first enforces nothing — a worker refused at checkout
    would raise its own ceiling and check out again.

    **`ledger.grant` IS THE CAPABILITY'S NAME, UNCHANGED FROM THE DRAW-DOWN
    MODEL.** The name predates the cap-set model this route now sets; renaming
    it would be a coordinated change across every token file that names it,
    including Orient's, and is out of scope here (`identity.py` carries the
    same note).

    **Each of `warn_usd`, `soft_cap_usd`, `hard_cap_usd` and `per_run_cap_usd`
    is optional and independently settable.** Omitting one, or sending it
    `null`, clears that threshold rather than leaving the previous value in
    place — a re-grant overwrites the whole cap set, the same "overwrites in
    place" behaviour the single grant always had. All four absent is a legal,
    uncapped ledger.

    **`per_run_cap_usd` is not a threshold on the project's total.** It bounds
    one admitted run's own spend, handed to that run in the checkout
    response's `budget.cap_usd` (issue_01M4BDRV8C47NQN5X8T82Y2B9A). It takes
    no part in the warn/soft/hard ordering, changes no ledger `state`, and
    `agentctl` is what enforces it, including the kill — this route only
    stores the number a human set.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "ledger.grant"):
        return refusals.forbidden("ledger.grant")
    if ident.kind != "approved_client":
        # UNREACHABLE while the table above is correct, and kept for the reason
        # `write_policy` keeps its twin: a capability set configured correctly
        # today is not a guarantee it stays that way.
        return refusals.forbidden("ledger.grant")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    human, attest_err = identity.attested_human(ident, payload)
    if attest_err:
        return refusals.refuse(403, attest_err["error"])
    scope_kind = payload.get("scope_kind")
    if scope_kind not in store.LEDGER_SCOPES:
        return refusals.refuse(
            400, "scope_kind must be one of the scopes a ledger may cover",
            hint="a run's cap is its project's per-run cap, delivered in "
                 "the checkout response and enforced by agentctl, so a run "
                 "is not a scope here",
            supported=sorted(store.LEDGER_SCOPES))
    scope_id = payload.get("scope_id")
    if not isinstance(scope_id, str) or not scope_id.strip():
        return refusals.refuse(400, "scope_id is required")
    scope_id = scope_id.strip()
    if len(scope_id) > store.MAX_LEDGER_SCOPE_ID:
        return refusals.refuse(
            400, f"scope_id is longer than {store.MAX_LEDGER_SCOPE_ID} characters")
    if scope_kind == "project" and not identity.in_scope(ident, scope_id):
        return refusals.not_found()
    refused, resolved_scope_id = await _refuse_unbookable_product(scope_id)
    if refused is not None:
        return refused
    # STORED UNDER THE RESOLVED PROJECT, never the caller's own spelling —
    # `_refuse_unbookable_product`'s docstring carries the reasoning and the
    # migration that moved every existing ledger onto this rule.
    scope_id = resolved_scope_id
    # **THE SCOPE IS CHECKED AGAIN ON THE LEDGER THIS WRITES.** The check
    # above runs on the caller's spelling, before the registry is consulted,
    # so an out-of-scope caller learns nothing from it. A project's ledger
    # carries every repository folded into it, so a grant naming one
    # repository must not set caps for the others (security review,
    # library#270 round 1).
    if scope_kind == "project" and not identity.in_scope(ident, scope_id):
        return refusals.not_found()
    # DOLLARS ON THE WIRE, MICRO-DOLLARS IN THE STORE. The caller speaks the
    # unit a human types; the column holds the exact integer a refusal is
    # computed from. Converting here rather than asking every caller to means
    # one place gets the rounding right.
    warn, err = _optional_amount(payload, "warn_usd")
    if err:
        return err
    soft_cap, err = _optional_amount(payload, "soft_cap_usd")
    if err:
        return err
    hard_cap, err = _optional_amount(payload, "hard_cap_usd")
    if err:
        return err
    per_run_cap, err = _optional_amount(payload, "per_run_cap_usd")
    if err:
        return err
    try:
        stored = await run_in_threadpool(store.upsert_ledger, {
            "scope_kind": scope_kind, "scope_id": scope_id,
            "warn_usd_micro": round(warn * 1_000_000) if warn is not None else None,
            "soft_cap_usd_micro": (round(soft_cap * 1_000_000)
                                   if soft_cap is not None else None),
            "hard_cap_usd_micro": (round(hard_cap * 1_000_000)
                                   if hard_cap is not None else None),
            "per_run_cap_usd_micro": (round(per_run_cap * 1_000_000)
                                      if per_run_cap is not None else None),
            "granted_by": human,
        })
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    except ValueError as e:
        return refusals.refuse(400, str(e))
    return {"id": stored["id"], "scope_kind": stored["scope_kind"],
            "scope_id": stored["scope_id"],
            "caps": {
                "warn_usd": (stored["warn_usd_micro"] / 1_000_000
                            if stored["warn_usd_micro"] is not None else None),
                "soft_cap_usd": (stored["soft_cap_usd_micro"] / 1_000_000
                                if stored["soft_cap_usd_micro"] is not None else None),
                "hard_cap_usd": (stored["hard_cap_usd_micro"] / 1_000_000
                                if stored["hard_cap_usd_micro"] is not None else None),
                "per_run_cap_usd": (stored["per_run_cap_usd_micro"] / 1_000_000
                                   if stored["per_run_cap_usd_micro"] is not None
                                   else None),
            },
            "acting_human": human, "attested_by": ident.actor}


async def _signal_ledger_threshold_crossed(ledger, day: str, total_micro: int,
                                           threshold: str) -> None:
    """The Archives record for a ledger newly crossing `threshold`.

    **`dedup_key` is `ledger-threshold:{ledger_id}:{day}:{threshold}`**, so this
    fires once per threshold per day rather than on every entry that lands
    past the line — `_crossed_threshold` already narrows the call to the entry
    that newly crosses it, and the dedup key is the second, independent
    guarantee against a race between two concurrent reports doing so at once.

    Best-effort and after the write commits, the same shape
    `_signal_course_status_anomaly` uses: a failure recording the signal must
    not undo the spend that already landed.
    """
    cap_column = {"warn": "warn_usd_micro", "soft_cap": "soft_cap_usd_micro",
                 "hard_cap": "hard_cap_usd_micro"}[threshold]
    cap_micro = ledger[cap_column]
    row = {
        "id": f"sig_{store.new_ulid()}",
        "source": "library",
        "kind": "ledger.threshold-crossed",
        "actor": ledger["scope_id"],
        "ingested_by": "library",
        "acting_human": None,
        "product": ledger["scope_id"],
        "stream": "chart-house",
        "body": f"the ledger for {ledger['scope_id']!r} crossed its "
                f"{threshold} threshold ({cap_micro / 1_000_000} USD) for "
                f"{day}, at a total of {total_micro / 1_000_000} USD",
        "refs": json.dumps({"ledger_id": ledger["id"]}),
        "meta": json.dumps({"threshold": threshold, "day": day,
                            "cap_usd": cap_micro / 1_000_000,
                            "total_usd": total_micro / 1_000_000}),
        "privacy_class": "internal",
        "dedup_key": f"ledger-threshold:{ledger['id']}:{day}:{threshold}",
        "produced_at": store.produced_now(),
    }
    try:
        await run_in_threadpool(store.insert_signal, row, lambda _ex: False)
    except Exception as e:                                     # noqa: BLE001
        print(f"library.ledger: could not record the threshold crossing: {e}",
              file=sys.stderr)


# The route's whole documented set (product, source, usd and day plus the
# seven optional attribution fields, plus `lane`). A caller naming anything
# outside it is refused by name rather than silently dropped — the
# ledger-store epic's clause 4 (issue_01M0B8TDRCH7SCGNSXHRGSV4TF): an
# attribution dimension no reader agreed to would otherwise vanish from the
# report with no sign it did.
_LEDGER_ENTRY_KEYS = frozenset({
    "product", "source", "usd", "day", "run_id", "agent", "model",
    "item_id", "person", "team", "dedup_key", "lane"})


@app.post("/ledger-entries")
async def report_spend(request: Request):
    """Report one spend event against a scope's ledger.

    **This never refuses on a capped ledger, and that is the design.** A
    spend that has already happened cannot be un-spent: two runs bypassing
    checkout draw a multiple of one cap, and the Library's job is to make the
    NEXT admission decision accurate rather than to keep the ledger tidy by
    losing the record of the money. The soft-cap refusal fires at checkout,
    against a ledger this route has already made honest.

    **A hard cap is recorded and published here, and revokes nothing yet.**
    The specification calls for the hard cap to revoke the tokens held by a
    project's active runs. `POST /tokens/redeem` exists now and issues library
    cards, but no route yet accepts a library card as a credential and
    `POST /tokens/revoke` (issue_01M12NE155ABV0K2K50P6CBWKJ) is unbuilt. So a
    hard-capped total is recorded, refuses admission the same way a soft cap
    does (see `_refuse_on_capped_ledger`), and is published as a
    `ledger.threshold-crossed` signal — and nothing here ends an
    already-running agent's card. Revocation attaches when that credential
    path and `POST /tokens/revoke` land.

    **`epic_id` is DERIVED FROM `item_id`, never accepted from the caller** —
    `store.record_spend` resolves it inside the same transaction as the
    insert, so a spend and the epic it is filed under cannot disagree. The operator's
    ruling (2026-09-15) is that the ledger carries only dimensions the system
    can state reliably, the same reasoning behind there being no `feature`
    field, and spend with no work item has no epic, so `epic_id` is NULL there
    rather than a caller's guess.

    **`day` is the CALLER's local day and is bounded, not trusted.** A gate must
    never trust a field its subject controls: unvalidated, a caller could book
    spend to whichever day still has headroom and the checkout refusal would
    never fire. One day either side of this machine's local date is wide enough
    for any real timezone offset and for clock skew, and narrow enough that no
    meaningful headroom is reachable by choosing a different date. Outside that,
    refused rather than clamped — silently rebucketing someone's spend is a
    worse answer than saying the clocks disagree.

    **`lane` is OPTIONAL, not yet required — the first of two contract steps
    (issue_01M35QG5NA13JE8FQVM9XET4HD).** `gateway` is metered provider
    spend; `subscription` is a flat-billed estimate. A value outside
    `store.LEDGER_LANES` is refused by name, the same shape every other
    closed set on this route already refuses with. A write naming no `lane`
    at all is still recorded, unchanged from today, with the column left
    NULL — refusing that is a later change, made once `agentctl` sends one.

    **An unset `lane` counts toward enforcement; `subscription` never
    does.** `_refuse_on_capped_ledger` and the `total_usd`/`state` this route
    returns all read `store.ledger_total`, which `store.record_spend` moves
    for a `gateway`-lane entry AND for one with no lane at all — only a
    `subscription`-lane entry is excluded. That is the conservative default
    for a dimension nothing requires yet: an entry nobody has tagged is not
    known to be the one lane that is not real money, and every entry ever
    written through this route before today carried no `lane` and was real,
    enforceable gateway spend. `GET /ledgers` is where the two lanes are
    reported apart.

    **`lane: "subscription"` ALSO NEEDS `ledger.spend.subscription`, because
    `ledger.spend` alone does not say who may assert the one lane nothing
    enforces.** `ledger.spend` sits on every `agent` and `system` credential;
    without a second gate, any of them could tag real, metered spend
    `"subscription"` to keep it out of `ledger_days` and every threshold and
    checkout check that reads it, while it still reports as recorded. Granted
    only through a credential entry's own `grants`
    (`identity.GRANT_ONLY_CAPS`), the same way `machine.verify` is — never
    through `ROLE_CAPS`, so minting the Bosun's reporter credential with it is
    a deliberate, auditable act rather than a role grant every agent and
    system credential inherits. `lane: "gateway"` and an absent `lane` need
    only `ledger.spend`, unchanged: both can only add to the enforced figure,
    which an unlaned report can already do today.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "ledger.spend"):
        return refusals.forbidden("ledger.spend")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    unknown = set(payload.keys()) - _LEDGER_ENTRY_KEYS
    if unknown:
        return refusals.refuse(
            400, f"unrecognised field(s) {sorted(unknown)}; only "
                 f"{sorted(_LEDGER_ENTRY_KEYS)} are accepted — no caller may "
                 f"invent its own attribution dimension")
    product = payload.get("product")
    if not isinstance(product, str) or not product.strip():
        return refusals.refuse(400, "product is required")
    product = product.strip()
    if len(product) > store.MAX_LEDGER_SCOPE_ID:
        return refusals.refuse(
            400, f"product is longer than {store.MAX_LEDGER_SCOPE_ID} characters")
    if not identity.in_scope(ident, product):
        return refusals.not_found()

    refused, resolved_product = await _refuse_unbookable_product(product)
    if refused is not None:
        return refused
    # THE LEDGER THIS BOOKS AGAINST IS THE PROJECT'S, never the repository
    # the caller happened to report — see `_refuse_unbookable_product`'s
    # docstring. The scope is checked twice. The check above runs on the
    # caller's spelling, before the registry is consulted, so an out-of-scope
    # caller learns nothing from it. This one runs on the ledger the spend
    # lands on, which carries every repository folded into the project, so
    # a grant naming one repository cannot move the others' total (security
    # review, library#270 round 1).
    product = resolved_product
    if not identity.in_scope(ident, product):
        return refusals.not_found()

    source = payload.get("source")
    if not isinstance(source, str) or source not in store.LEDGER_SOURCES:
        return refusals.refuse(
            400, "source must name which half reported this spend",
            supported=sorted(store.LEDGER_SOURCES))
    lane = payload.get("lane")
    if lane is not None and (not isinstance(lane, str)
                             or lane not in store.LEDGER_LANES):
        return refusals.refuse(
            400, "lane must name which kind of spend this is, or be absent",
            supported=sorted(store.LEDGER_LANES))
    if lane == "subscription" and not identity.authorized(
            ident, "ledger.spend.subscription"):
        return refusals.forbidden("ledger.spend.subscription")
    usd, err = _amount(payload, "usd")
    if err:
        return err
    day = payload.get("day") or local_day()
    if not store._day_shape_ok(day):
        return refusals.refuse(400, "day must be YYYY-MM-DD")
    today = local_day()
    if not _within_a_day(day, today):
        return refusals.refuse(
            422, "day is too far from this Library's own date",
            hint="a spend is booked to the day it happened, within a day of "
                 "the Library's local date; a wider window would let a caller "
                 "choose a day that still has headroom",
            day=day, library_day=today)
    for field in ("run_id", "agent", "model", "item_id", "person", "team",
                  "dedup_key"):
        value = payload.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            return refusals.refuse(400, f"{field} must be a non-empty string "
                                        f"when present")
    conn = store.connect()
    try:
        ledger = store.live_ledger(conn, "project", product)
    finally:
        conn.close()
    if ledger is None:
        # AN UNCAPPED PROJECT IS THE NORMAL CASE, and its spend is recorded
        # like any other's — only a cap is missing, not a ledger row.
        # `ensure_live_ledger` creates one with all three caps null the first
        # time a product reports with none, which is a legal, already-served
        # state (`PUT /ledgers` makes the same row by hand). `live_ledger`
        # itself keeps answering None for a scope nobody has reported or
        # capped yet — admission's "a missing ledger is not a refusal" and
        # `GET /ledgers` both depend on that staying true.
        ledger = await run_in_threadpool(store.ensure_live_ledger, "project", product)
    try:
        idempotent, entry, crossed = await run_in_threadpool(store.record_spend, {
            "ledger_id": ledger["id"], "day": day,
            "usd_micro": round(usd * 1_000_000), "source": source, "lane": lane,
            "product": product, "reported_by": ident.actor,
            "run_id": _opt(payload, "run_id"), "agent": _opt(payload, "agent"),
            "model": _opt(payload, "model"), "item_id": _opt(payload, "item_id"),
            "person": _opt(payload, "person"), "team": _opt(payload, "team"),
            "dedup_key": _opt(payload, "dedup_key"),
        })
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    except store.DisproportionateSpend as e:
        # THE ONE REFUSAL THAT NAMES ITS NUMBERS, because a caller told only
        # "too large" cannot tell a wrong ceiling from a wrong report, and
        # those need opposite fixes. `e.basis` says which rule computed the
        # ceiling, so the caller learns not just the number but why it is the
        # bound.
        basis_phrase = {
            "soft_cap": "10x the scope's soft cap",
            "hard_cap": "10x the scope's hard cap",
            "uncapped": "$1,000, the flat ceiling for a scope with no "
                        "refusing cap set",
        }[e.basis]
        return refusals.refuse(
            422, str(e),
            hint=f"one report may not exceed {basis_phrase}. A genuine "
                 "overrun is fine and is recorded; a report this size means "
                 "the cap is wrong or something upstream is broken, and a "
                 "human should see it before it lands in the store that "
                 "gates admission",
            reported_usd=e.reported_micro / 1_000_000,
            basis=e.basis,
            basis_usd=e.basis_micro / 1_000_000 if e.basis_micro is not None else None,
            maximum_single_report_usd=e.ceiling_micro / 1_000_000)
    except store.DedupKeyTaken as e:
        # NOTHING OF THE MATCHED ROW IN THIS BODY (CQ·107): `e` carries only
        # the key, so there is nothing here to leak from the other ledger's
        # entry — no id, day, usd, lane or ledger_id of it.
        return refusals.refuse(
            409, str(e),
            hint="send a different dedup_key for this report")
    except ValueError as e:
        return refusals.refuse(400, str(e))
    conn = store.connect()
    try:
        total = store.ledger_total(conn, ledger["id"], entry["day"])
    finally:
        conn.close()
    if crossed is not None:
        await _signal_ledger_threshold_crossed(ledger, entry["day"], total, crossed)
    return {"recorded": True, "idempotent": idempotent, "id": entry["id"],
            "ledger_id": ledger["id"], "day": entry["day"],
            "usd": entry["usd_micro"] / 1_000_000, "lane": entry["lane"],
            "total_usd": total / 1_000_000,
            "state": store.ledger_state(ledger, total),
            "crossed": crossed, "epic_id": entry["epic_id"]}


@app.post("/ledger-entries/{entry_id}/void")
async def void_ledger_entry(request: Request, entry_id: str):
    """Reverse one spend entry, correcting the day's total. Orient-only-write.

    **Reserved to `approved_client` through `ledger.void`, for the reason
    `identity.py` records beside the capability**: voiding corrects the record
    that gates admission, so an agent able to void its own spend could spend
    past a cap and then erase the evidence — the same "a gate must never trust
    a field its subject controls" rule the ledger's day key already follows.
    The same three-part shape `PUT /ledgers` uses: the capability check, the
    unconditional `ident.kind` refusal belt-and-suspenders, and the acting
    human resolved by `attested_human` rather than claimed by the caller.

    **Idempotent.** Voiding an already-voided entry answers 200 with the row
    unchanged and `already_voided: true` — the same shape a duplicate
    `dedup_key` gets on the way in.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "ledger.void"):
        return refusals.forbidden("ledger.void")
    if ident.kind != "approved_client":
        # UNREACHABLE while the table above is correct, kept for
        # `grant_ledger`'s reason.
        return refusals.forbidden("ledger.void")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    human, attest_err = identity.attested_human(ident, payload)
    if attest_err:
        return refusals.refuse(403, attest_err["error"])
    reason = payload.get("reason")
    if reason is not None and not isinstance(reason, str):
        return refusals.refuse(400, "reason must be a string when present")
    conn = store.connect()
    try:
        entry = conn.execute("SELECT product FROM ledger_entries WHERE id = ?",
                             (entry_id,)).fetchone()
    finally:
        conn.close()
    # SCOPED BEFORE THE WRITE, the same order `report_spend` checks `product`
    # in. A caller out of scope for this entry's product must not learn
    # whether the id exists, so this answers `not_found` rather than 404 vs
    # 403 splitting the two.
    if entry is None or not identity.in_scope(ident, entry["product"]):
        return refusals.not_found()
    try:
        already_voided, stored = await run_in_threadpool(
            store.void_spend_entry, entry_id, human, _opt(payload, "reason"))
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    except LookupError:
        return refusals.not_found()
    except ValueError as e:
        return refusals.refuse(400, str(e))
    conn = store.connect()
    try:
        ledger = conn.execute("SELECT * FROM ledgers WHERE id = ?",
                              (stored["ledger_id"],)).fetchone()
        total = store.ledger_total(conn, stored["ledger_id"], stored["day"])
    finally:
        conn.close()
    return {"already_voided": already_voided,
            "entry": store.ledger_entry_dict(stored),
            "total_usd": total / 1_000_000,
            "state": store.ledger_state(ledger, total) if ledger else None,
            "acting_human": human, "attested_by": ident.actor}


@app.get("/runs")
async def list_runs(request: Request):
    """What is running right now, pulled from the Bosun and served near-live.

    **The Library is the only thing Deck and Orient talk to**, which is the whole
    point of the chain pull: they read this route, the Library reads the Bosun's
    register, and neither client becomes a second client of the Bosun.

    **`stale_after_seconds` is stated rather than left implicit.** The Bosun's
    read is sub-millisecond and uncached, so the Library's bound IS the freshness
    a client sees — it is the only staleness in the whole chain. A client that
    has to guess how old this is will guess wrong.

    **An unreachable register answers 200 with `available: false` and a reason.**
    The Bosun's job ships deliberately inactive, so absence is a normal state
    rather than an error, and a 503 here would take out a crew view over a
    dependency that is optional by design.

    **Scoped by the run's repo, like every other list route.** A run's `repo`
    resolves to a product and an out-of-scope caller does not see it. That
    matters more here than on most routes because the register carries `task`
    text and `worktree` paths, which for a private crew is exactly the content
    ADR-0043's wall exists to keep in.

    **NO COST FIELD YET, deliberately.** The epic anticipates joining spend from
    the Library's own ledger rather than asking the Bosun for a second cost path,
    and that ledger now exists — but nothing writes to it (the Bosun's writer is
    `issue_01M0TP37M3RRRY5BD1Z4B8GCB0`). Every run would therefore read
    `$0.00`, which a viewer reads as "this run cost nothing" rather than "nobody
    has reported yet". A wrong number is worse than an absent one. Add the join
    when a writer exists.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "item.read"):
        return refusals.forbidden("item.read")
    if (err := _reject_unknown_params(request, set())):
        return err

    # **BLACK-BOX CREWS ARE EXCLUDED BEFORE ANYTHING ELSE, AND AN UNREADABLE
    # REGISTRY SERVES NOTHING** (security-reviewer, PR #51, blocking). ADR-0043:
    # a private crew is "registered so the platform knows it exists, with none of
    # its content reaching the Library and none of its agents visible to Orient."
    # Its AGENTS, not merely its task text — so the row goes, rather than a field
    # being redacted. `cfo` is marked `private: true` with
    # `task_text_public: false`, which agrees rather than softening it.
    #
    # **This fails CLOSED, inverting this module's usual trade.** `projects.py`
    # fails open everywhere else on the reasoning that a service which cannot
    # accept a filing is a work stoppage while a wrongly-scoped row is fixable.
    # That reasoning does not survive here: the question is "may I show this to
    # somebody", and a wrong answer discloses a private crew's live task text and
    # worktree path, which no later edit can take back. `private_repos()`
    # returns None rather than an empty set precisely so this can tell "nothing
    # is private" from "I could not find out".
    private = await run_in_threadpool(projects.private_repos)
    if private is None:
        return {"runs": [], "available": False,
                "reason": "the project registry could not be read, so private "
                          "crews cannot be identified and nothing is served",
                "generated_at": None,
                "stale_after_seconds": runs.TTL_SECONDS, "count": 0}

    snap = await run_in_threadpool(runs.snapshot)

    # **A SHALLOW COPY PER ROW, because `snapshot()` hands back the cached
    # object itself** (both reviewers, PR #51). Mutating it in place wrote a
    # route-specific field onto a module-owned cache shared by every request
    # inside the TTL. It happened not to leak while `holds` was recomputed
    # unconditionally — and the holder-scope fix below makes `holds` depend on
    # the CALLER, which is exactly when a shared object becomes a real
    # cross-request leak. Copying now rather than discovering that later.
    visible = [dict(r) for r in snap["runs"]
               if r.get("repo")
               and r["repo"].strip().lower() not in private
               and identity.in_scope(ident, r["repo"])]

    # THE HOLDER JOIN, from the Library's own records — a lease has carried
    # `run_id` since Keel Phase 2, so "what is running" and "who holds which
    # epic" line up without asking the Bosun for anything.
    #
    # **JOINED THROUGH `issue_items` AND SCOPED ON THE ITEM'S OWN PRODUCT**
    # (security-reviewer, PR #51, blocking). The first version scoped the RUN by
    # the Bosun's `repo` string and then returned whatever item was leased
    # against it, unchecked. A run's `repo` and an item's `product` are
    # different fields and nothing makes them agree, so a caller in scope for
    # one could learn the id of an item it may not see — and get a working link
    # to it. `get_item` refuses that id on the next request, by which point the
    # id and its existence have already left. That route's own comment names
    # this failure: splitting "no such item" from "not yours" turns the pair
    # into an existence oracle. Every sibling route that touches `issue_leases`
    # scopes through `issue_items`; this one now does too.
    if visible:
        run_ids = [r["run_id"] for r in visible if r.get("run_id")]
        if run_ids:
            def _held():
                conn = store.connect()
                try:
                    scope, scope_params = _scope_clause(ident)
                    rows = conn.execute(
                        f"SELECT issue_leases.run_id, issue_leases.root_id "
                        f"FROM issue_leases "
                        f"JOIN issue_items ON issue_items.id = issue_leases.root_id "
                        f"WHERE issue_leases.released_at IS NULL "
                        f"AND issue_leases.run_id IN "
                        f"({','.join('?' * len(run_ids))}){scope}",
                        [*run_ids, *scope_params]).fetchall()
                    return {r["run_id"]: r["root_id"] for r in rows}
                finally:
                    conn.close()
            # THREADPOOLED like every other DB read in this file (reviewer,
            # PR #51). Run inline it blocks the event loop, and therefore every
            # other concurrent request on this worker, for the query's duration.
            held = await run_in_threadpool(_held)
        else:
            held = {}
        for r in visible:
            root = held.get(r.get("run_id"))
            r["holds"] = ({"item_id": root,
                           "url": f"/engineering/issues/{root}"} if root else None)

    return {"runs": visible,
            "available": snap["available"],
            "reason": snap["reason"],
            "generated_at": snap.get("generated_at"),
            "stale_after_seconds": runs.TTL_SECONDS,
            "count": len(visible)}


@app.get("/ledger-entries")
async def list_ledger_entries(request: Request):
    """The individual spend entries behind a ledger's total.

    `GET /ledgers` answers "how much has this project spent today"; nothing
    answered "on what" until this route — the display half of the ledger epic
    (`issue_01M0B8YCFN4C1CYNANVG87CANG`). Same capability as the total,
    `ledger.read`: reading one entry is not a different privilege than reading
    the total it feeds.

    **Scoped in SQL, like every other list here** — `_scope_clause`'s own
    docstring names the failure a post-filter would cause: `total` counting rows
    the caller may not see, and a page coming back shorter than the count a
    client pages by. This table's product column IS `product`, so the shared
    clause applies directly — unlike `GET /ledgers`, whose column is `scope_id`
    and hand-writes its own.

    **A malformed `day`/`from`/`to`, or an unrecognised `source`, is REFUSED
    rather than dropped.** Silently ignoring a bad filter widens the result set,
    which is the wrong direction for a route that exists to answer "what did we
    spend on".

    **Newest spend first**, `at DESC`, with `id` breaking a tie — matching the
    rule every other ordered query in this file follows, so a row landing on two
    pages or on neither under LIMIT/OFFSET is not left to whatever order SQLite
    happens to return ties in.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "ledger.read"):
        return refusals.forbidden("ledger.read")
    if (err := _reject_unknown_params(request, {
            "limit", "offset", "product", "day", "from", "to", "source",
            "run_id", "item_id", "epic_id"})):
        return err
    limit, offset, err = _page(request)
    if err:
        return err
    where, params = "WHERE 1 = 1", []
    scope, scope_params = _scope_clause(ident)
    where += scope
    params += scope_params
    if (product := request.query_params.get("product")):
        # RESOLVED THE SAME WAY `GET /ledgers`' `scope_id` filter is: every
        # stored `product` is now the resolved project, so a caller
        # filtering by a repository name gets resolved rather than matching
        # nothing.
        product = store.ledger_scope_name(product)
        where += " AND product = ?"
        params.append(product)
    day = request.query_params.get("day")
    if day is not None:
        if not store._day_shape_ok(day):
            return refusals.refuse(400, "day must be YYYY-MM-DD")
        where += " AND day = ?"
        params.append(day)
    day_from = request.query_params.get("from")
    if day_from is not None:
        if not store._day_shape_ok(day_from):
            return refusals.refuse(400, "from must be YYYY-MM-DD")
        where += " AND day >= ?"
        params.append(day_from)
    day_to = request.query_params.get("to")
    if day_to is not None:
        if not store._day_shape_ok(day_to):
            return refusals.refuse(400, "to must be YYYY-MM-DD")
        where += " AND day <= ?"
        params.append(day_to)
    source = request.query_params.get("source")
    if source is not None:
        if source not in store.LEDGER_SOURCES:
            return refusals.refuse(
                400, "source must name which half reported this spend",
                supported=sorted(store.LEDGER_SOURCES))
        where += " AND source = ?"
        params.append(source)
    if (run_id := request.query_params.get("run_id")):
        where += " AND run_id = ?"
        params.append(run_id)
    if (item_id := request.query_params.get("item_id")):
        where += " AND item_id = ?"
        params.append(item_id)
    if (epic_id := request.query_params.get("epic_id")):
        where += " AND epic_id = ?"
        params.append(epic_id)
    conn = store.connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) c FROM ledger_entries {where}", params).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM ledger_entries {where} ORDER BY at DESC, id "
            f"LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
        out = [store.ledger_entry_dict(r) for r in rows]
    finally:
        conn.close()
    return {"entries": out, "count": len(out), "total": total, "offset": offset,
            "truncated": offset + len(out) < total}


def _validate_merge_identity(payload: dict):
    """`repo`, `pr`, `head_sha` and `requester` — the tuple naming which merge
    a call is about. Shared between `/merge/admission` and `/merge/outcome`
    so the two refuse a malformed name identically, and returns `(fields,
    None)` or `(None, refusal)`.

    **`pr` is refused the same way `POST /items/{id}/link_pr` already refuses
    it**: a number or a non-empty string, never a bool, never absent, and a
    string form is clipped to `store.MAX_REF` exactly as `link_pr_to_item`
    clips it. Same caller-controlled shape, same failure mode — a GitHub PR
    number a client may reasonably send either way.
    """
    for field in ("repo", "head_sha", "requester"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            return None, refusals.refuse(400, f"{field} is required")
    pr = payload.get("pr")
    if isinstance(pr, bool) or pr is None:
        return None, refusals.refuse(400, "pr is required")
    if not isinstance(pr, (int, str)):
        return None, refusals.refuse(400, "pr must be a number or string")
    if isinstance(pr, str) and not pr.strip():
        return None, refusals.refuse(400, "pr is required")
    return {
        "repo": store.clip(payload["repo"].strip(), store.MAX_IDENT),
        "pr": store.clip(pr.strip(), store.MAX_REF) if isinstance(pr, str) else pr,
        "head_sha": store.clip(payload["head_sha"].strip(), store.MAX_REF),
        "requester": store.clip(payload["requester"].strip(), store.MAX_ACTOR),
    }, None


def _merge_repo_scope(ident: identity.Identity, repo: str) -> JSONResponse | None:
    """404s a merge call whose caller is not scoped to `repo`'s product — the
    same wording `POST /signals` gives an out-of-scope caller, so the pair is
    not an existence oracle.

    `identity.authorized` is purely capability-based, with no product
    parameter, so scope has to be checked here, against the same
    `projects.py` registry `POST /signals` and `/runs` resolve through.

    **Fails CLOSED, unlike `projects.resolve()` on its own.** `resolve()`
    returns its input unchanged when the registry can't be read, which would
    turn an unreadable registry into a pass-through for every caller here.
    `projects.available()` is checked first so that case refuses instead,
    matching `/runs`'s treatment of `private_repos() is None`.

    `repo` arrives as `owner/name` (`convivy/library`), and the registry's
    own `repos` entries are the bare repo name (`library`), so only the
    segment after the last `/` is resolved.
    """
    if not projects.available():
        return refusals.not_found()
    name = repo.rsplit("/", 1)[-1]
    product = projects.resolve(name)
    if product is None or not identity.in_scope(ident, product):
        return refusals.not_found()
    return None


def _record_merge_signal(ident: identity.Identity, *, source: str, repo: str,
                         pr, head_sha: str, requester: str,
                         extra_meta: dict) -> bool:
    """The Archives signal both merge routes write on every call — loud
    enough that somebody notices, matching `identity.describe_credentials`'s
    reasoning that a posture nobody can see is a posture nobody checks.

    `kind` is the same literal, `"merge_admission_stub_call"`, for a call to
    either route — `source` (`"merge_admission"` or `"merge_outcome"`) is
    what tells the two apart in the Archives.

    **No `dedup_key`.** Every call is a distinct event — a second admission
    check for the same `head_sha`, or a retried outcome report, is still
    something that happened — and `insert_signal`'s own comment already
    explains that a writer with nothing to key on simply writes none rather
    than invent a key that would fold distinct events into one.

    **Best-effort, matching `_embed_after_commit`'s reasoning rather than
    `POST /signals`'s own.** There, a capture IS the deliverable and
    `WritesDisabled` is correctly a 503 — the caller asked this service to
    write something and it could not. Here the signal is a side effect of an
    answer the caller already has in hand: `/merge/admission` has already
    decided `approved`/`reason` before this runs, and `/merge/outcome` has
    nothing left to decide once its body validates. `insert_signal` goes
    through `connect_rw`, which raises `WritesDisabled` whenever
    `LIBRARY_WRITES_ENABLED` is unset — which is every deploy today, for the
    whole read-only shadow §4 describes. Making that flag gate the merge
    routes too would mean `/merge/admission` refuses every call for the
    entire shadow period, on the one route this epic exists to give the
    Bosun something to ask right now. So a failed write here is logged to
    stderr and swallowed rather than turned into a refusal or a 500: a
    missing audit row is a real cost, and a merge gate that stopped
    answering because an unrelated audit table is not the write path yet
    would be a worse one.

    Returns whether the signal was actually written, so `/merge/outcome` can
    say so rather than asserting a record that may have been lost.
    """
    row = {
        "id": f"sig_{store.new_ulid()}",
        "source": source,
        "kind": "merge_admission_stub_call",
        "actor": ident.actor,
        "ingested_by": ident.actor,
        "acting_human": None,
        "product": repo,
        "stream": None,
        "body": f"{source}: {repo}#{pr} requested by {requester}",
        "refs": json.dumps({"repo": repo, "pr": pr}),
        "meta": json.dumps({"repo": repo, "pr": pr, "head_sha": head_sha,
                            "requester": requester, **extra_meta}),
        "privacy_class": "internal",
        "dedup_key": None,
        "produced_at": store.produced_now(),
    }
    try:
        store.insert_signal(row, lambda _existing: False)
        return True
    except store.WritesDisabled as exc:
        print(f"library: merge admission signal NOT recorded for "
              f"{repo}#{pr} ({source}): {exc}", file=sys.stderr)
        return False


@app.post("/merge/admission")
async def merge_admission(request: Request):
    """May this merge happen — the Library's half of merge admission
    (`issue_01M0EQTMZMAB93V9FFP4AD52JK`, epic `issue_01KZCGQ43C0W6TBYTPDMR0HNVG`).

    **Always operator-initiated; no agent here decides to merge.** An operator
    presses a merge button in Deck or Orient, the Bosun is asked to merge, and
    the Bosun asks this route "may this merge happen?" before it does. On
    `approved: true` the Bosun merges and reports the result to `POST
    /merge/outcome`.

    **`LIBRARY_MERGE_ADMISSION_ENFORCED` is the whole switch, and both
    branches answer the same request and response shape** — `{approved,
    reason}` — so flipping it is a Library-only deploy change the Bosun never
    has to coordinate with. A gate that approves everything is
    indistinguishable from a real one to anyone downstream, which is what
    lets this turn real on its own schedule rather than late, or never,
    waiting for a coordinated release.

    Unset — today's default, and everywhere until a real evaluator lands —
    this approves every request: `{"approved": true, "reason": "stub: merge
    admission not yet enforced"}`. **Set, no evaluator exists yet, so this
    answers an honest refusal instead of quietly keeping the stub's answer**:
    `{"approved": false, "reason": "..."}` naming that enforcement is
    configured but nothing evaluates it. An operator who sets the flag today
    expecting a real gate gets a refusal that says so, not an approval that
    looks identical to the one the flag was supposed to replace.

    See the comment at `store.MERGE_ADMISSION_ENFORCED` for why the default
    must stay permissive until the same change that builds the evaluator.

    **Exclusivity — one merge at a time — is out of scope.** It waits on a
    GitHub App credential this build does not have; this route does not
    pretend to serialize concurrent merges.

    **Not human-reserved**, the same reasoning `/reference/admission` gives:
    this is a deterministic (today, stubbed) check, not an authorization only
    a human may grant, so every `agent`/`system` identity holding
    `merge.admit` may ask it.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "merge.admit"):
        return refusals.forbidden("merge.admit")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    fields, err = _validate_merge_identity(payload)
    if err:
        return err
    if (err := _merge_repo_scope(ident, fields["repo"])):
        return err
    if store.MERGE_ADMISSION_ENFORCED:
        result = {
            "approved": False,
            "reason": "stub: LIBRARY_MERGE_ADMISSION_ENFORCED is set, but no "
                      "evaluator is built yet — refusing rather than "
                      "approving blind",
        }
    else:
        result = {"approved": True,
                  "reason": "stub: merge admission not yet enforced"}
    await run_in_threadpool(
        _record_merge_signal, ident, source="merge_admission", **fields,
        extra_meta={"enforced": store.MERGE_ADMISSION_ENFORCED,
                    "approved": result["approved"]})
    return result


@app.post("/merge/outcome")
async def merge_outcome(request: Request):
    """The merge result, reported back by the Bosun after `/merge/admission`
    approved it — the reconciliation hook `issue_01M0EQTMZMAB93V9FFP4AD52JK`
    exists to give it.

    **Reconciliation is a deliberate no-op.** Releasing the lease the merge
    held, moving the work item out of `pr-open`, and bead accounting are real
    design work this epic does not do — this route validates the body,
    records the Archives signal every merge-admission call writes, and
    answers. It does not touch a lease, a work item, or a bead; a caller must
    not assume any of those three update as a side effect of a call landing
    here today. Wiring reconciliation is a later, separate change.

    Same identifying tuple as `/merge/admission` — `repo`, `pr`, `head_sha`,
    `requester` — plus `merged`: whether the merge actually landed. `reason`
    is optional context, most useful when `merged` is false.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "merge.report"):
        return refusals.forbidden("merge.report")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")
    fields, err = _validate_merge_identity(payload)
    if err:
        return err
    if (err := _merge_repo_scope(ident, fields["repo"])):
        return err
    merged = payload.get("merged")
    if not isinstance(merged, bool):
        return refusals.refuse(400, "merged must be a boolean")
    reason = payload.get("reason")
    if reason is not None and not isinstance(reason, str):
        return refusals.refuse(400, "reason must be a string when present")
    reason = store.clip(reason.strip(), store.MAX_ITEM_TEXT) if reason else None
    recorded = await run_in_threadpool(
        _record_merge_signal, ident, source="merge_outcome", **fields,
        extra_meta={"merged": merged, "reason": reason})
    return {"recorded": recorded}


# ---------------------------------------------------------------------------
# POST /review-records: the review-record write route
# (issue_01M2HS3Q36RE5F82QXRN4Z0D0K), the Library's half of "All reviews are
# memory" (issue_01M2HS6XNVPG6ZB5RNQDN2TJKN) after the schema
# (issue_01M2HS394KTFMRATFK1N3G4ZNB, `468a1789d29e` and `e4894432e1aa`).
# ---------------------------------------------------------------------------

# `github:<owner>/<repo>` exactly — no extra path segments, no whitespace.
_REVIEW_GITHUB_IDENTITY_RE = re.compile(r"^github:([^\s/]+)/([^\s/]+)$")
# `git:<40 lowercase hex>` — a root-commit sha, for a repository with no
# GitHub remote.
_REVIEW_GIT_IDENTITY_RE = re.compile(r"^git:([0-9a-f]{40})$")
# The commit or document hash a review covers: a git sha (40 hex) or a
# document content hash (64 hex, sha256).
_REVIEWED_REF_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")


def _parse_repo_identity(raw: str | None):
    """`(scheme, bare_repo, repo_identity, reason)` for a `repo_identity`
    string — the structural check (required, bounded, matches one of the
    two schemes) that both the write route's payload field and the read
    routes' query parameter must apply identically, so the same string is
    never valid on one side of the contract and invalid on the other.

    `reason` is `None` on success, else one of `"required"`, `"too_long"`
    or `"malformed"`, naming which check failed so each caller builds its
    own refusal message and status code — this function owns only the
    parse, not the wording. `repo_identity` (the stripped string) is
    still returned alongside `"too_long"` or `"malformed"`, since a
    caller's refusal message names the value that failed; only
    `"required"` has no string worth naming.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, None, None, "required"
    repo_identity = raw.strip()
    if len(repo_identity) > store.MAX_REVIEW_REPO_IDENTITY:
        return None, None, repo_identity, "too_long"
    github_match = _REVIEW_GITHUB_IDENTITY_RE.match(repo_identity)
    git_match = _REVIEW_GIT_IDENTITY_RE.match(repo_identity)
    if not github_match and not git_match:
        return None, None, repo_identity, "malformed"
    scheme = "github" if github_match else "git"
    bare_repo = github_match.group(2) if github_match else None
    return scheme, bare_repo, repo_identity, None


def _validate_review_record_payload(payload: dict):
    """The write route's caller-supplied fields, structurally validated and
    bounded — everything that does not need a repository resolved, a run
    checked, or a database open. Returns `(fields, refusal)`.

    **Every list-and-string field is bounded here, refused rather than
    clipped** — the same `POST /signals` convention `create_signal`'s own
    comment explains: a route that clipped silently would turn an oversized
    field into quiet data loss instead of a caller-visible refusal.
    """
    scheme, bare_repo, repo_identity, reason = _parse_repo_identity(
        payload.get("repo_identity"))
    if reason == "required":
        return None, refusals.refuse(400, "repo_identity is required")
    if reason == "too_long":
        return None, refusals.refuse(
            400, f"repo_identity is longer than "
                 f"{store.MAX_REVIEW_REPO_IDENTITY} characters")
    if reason == "malformed":
        return None, refusals.refuse(
            400, "repo_identity must be exactly github:<owner>/<repo> or "
                 "git:<40 lowercase hex characters>",
            repo_identity=repo_identity)

    reviewed_ref = payload.get("reviewed_ref")
    if not isinstance(reviewed_ref, str) or not _REVIEWED_REF_RE.match(reviewed_ref):
        return None, refusals.refuse(
            400, "reviewed_ref must be 40 or 64 lowercase hex characters")

    role = payload.get("role")
    if not isinstance(role, str) or not role.strip():
        return None, refusals.refuse(400, "role is required")
    role = role.strip()
    if len(role) > store.MAX_REVIEW_ROLE:
        return None, refusals.refuse(
            400, f"role is longer than {store.MAX_REVIEW_ROLE} characters")

    verdict = payload.get("verdict")
    if not isinstance(verdict, str) or not verdict.strip():
        return None, refusals.refuse(400, "verdict is required")
    verdict = verdict.strip()
    if len(verdict) > store.MAX_REVIEW_VERDICT:
        return None, refusals.refuse(
            400, f"verdict is longer than {store.MAX_REVIEW_VERDICT} characters")

    # Only an absent key or a JSON null means "no prose", the shape
    # `POST /signals` gives `body` (library#201). `or ""` coerced 0, false,
    # [] and {} to "" before the type check ran, so each was stored as
    # empty prose with a 201 (issue_01M3XPJX701JSV60GNM5ZK0NJ0).
    prose = payload.get("prose")
    if prose is None:
        prose = ""
    elif not isinstance(prose, str):
        return None, refusals.refuse(400, "prose must be a string")
    if len(prose) > store.MAX_TEXT:
        return None, refusals.refuse(
            400, f"prose is longer than {store.MAX_TEXT} characters")

    pr = payload.get("pr")
    if pr is not None and (isinstance(pr, bool) or not isinstance(pr, int)):
        return None, refusals.refuse(400, "pr must be a number when present")

    run_id = payload.get("run_id")
    if run_id is not None:
        if not isinstance(run_id, str) or not run_id.strip():
            return None, refusals.refuse(
                400, "run_id must be a non-empty string when present")
        run_id = run_id.strip()
        if len(run_id) > store.MAX_IDENT:
            return None, refusals.refuse(
                400, f"run_id is longer than {store.MAX_IDENT} characters")

    source_comment_id = payload.get("source_comment_id")
    if source_comment_id is not None:
        if not isinstance(source_comment_id, str) or not source_comment_id.strip():
            return None, refusals.refuse(
                400, "source_comment_id must be a non-empty string when present")
        source_comment_id = source_comment_id.strip()
        if len(source_comment_id) > store.MAX_IDENT:
            return None, refusals.refuse(
                400, f"source_comment_id is longer than {store.MAX_IDENT} "
                     f"characters")

    payload_product = payload.get("product")
    if payload_product is not None and not isinstance(payload_product, str):
        return None, refusals.refuse(400, "product must be a string when present")

    findings = payload.get("findings")
    if findings is None:
        findings = []
    if (not isinstance(findings, list)
            or not all(isinstance(f, dict) for f in findings)):
        return None, refusals.refuse(400, "findings must be an array of objects")
    if len(findings) > store.MAX_REVIEW_FINDINGS:
        return None, refusals.refuse(
            400, f"at most {store.MAX_REVIEW_FINDINGS} findings per record",
            count=len(findings))
    for i, finding in enumerate(findings):
        if len(json.dumps(finding)) > store.MAX_TEXT:
            return None, refusals.refuse(
                400, f"findings[{i}] is longer than {store.MAX_TEXT} "
                     f"characters", index=i)
    if len(json.dumps(findings)) > store.MAX_TEXT:
        return None, refusals.refuse(
            400, f"findings is longer than {store.MAX_TEXT} characters")

    string_lists = {}
    for field, list_cap, str_cap in (
            ("paths", store.MAX_REVIEW_PATHS, store.MAX_REVIEW_PATH),
            ("doc_ids", store.MAX_REVIEW_DOC_IDS, store.MAX_REVIEW_DOC_ID),
            ("item_ids", store.MAX_REVIEW_ITEM_IDS, store.MAX_REVIEW_ITEM_ID)):
        value = payload.get(field)
        if value is None:
            value = []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return None, refusals.refuse(400, f"{field} must be an array of strings")
        if len(value) > list_cap:
            return None, refusals.refuse(
                400, f"at most {list_cap} {field} entries", count=len(value))
        for entry in value:
            if len(entry) > str_cap:
                return None, refusals.refuse(
                    400, f"a {field} entry is longer than {str_cap} characters")
        string_lists[field] = value

    auth_claim = payload.get("auth")
    if auth_claim is not None and auth_claim not in store.REVIEW_AUTH_VALUES:
        # **THE CALLER'S `auth` IS REFUSED WHEN UNRECOGNISED, AND OTHERWISE
        # IGNORED** — never trusted, matching the epic's ruling that the
        # Library alone decides `auth`. Refusing an unrecognised value here
        # rather than silently discarding it catches a caller's typo before
        # it reaches a route that would have quietly ignored the intent
        # behind it.
        return None, refusals.refuse(
            400, "auth is not one of the values this vault defines",
            auth=auth_claim, supported=sorted(store.REVIEW_AUTH_VALUES))

    return {
        "repo_identity": repo_identity, "scheme": scheme, "bare_repo": bare_repo,
        "reviewed_ref": reviewed_ref, "role": role, "verdict": verdict,
        "prose": prose, "pr": pr, "run_id": run_id,
        "source_comment_id": source_comment_id,
        "payload_product": payload_product, "findings": findings,
        **string_lists,
    }, None


def _review_record_product(ident: identity.Identity, fields: dict,
                           facts: dict | None) -> tuple[str | None, JSONResponse | None]:
    """The product a review record is scoped to, derived from `repo_identity`
    through the crew registry — never taken from the payload
    (issue_01M2HS3Q36RE5F82QXRN4Z0D0K).

    **`github:` resolves through the registry directly.** `git:` always
    comes from the caller's own `product` — itself resolved through the
    registry, never trusted as a slug — whether or not the payload names a
    run. `_review_record_auth` never grants a `git:` identity `run-verified`
    (`run_facts` carries no root commit to bind one against), so this
    product is only a read-scope label for a `git:` record, never something
    a gate could rely on to have been checked against the run.

    Fails the same way `_merge_repo_scope` does when the registry itself is
    unreadable: `projects.available()` first, so an unreadable file refuses
    every caller here rather than passing every product string through
    unchecked.
    """
    if not projects.available():
        return None, refusals.not_found()
    if fields["scheme"] == "github":
        product = projects.resolve(fields["bare_repo"])
    else:
        if fields["payload_product"] is None:
            return None, refusals.refuse(
                400, "product is required for a git: repo_identity")
        product = projects.resolve(fields["payload_product"])
    if product is None:
        return None, refusals.not_found()
    # For `git:`, `product` above already came from this same
    # `payload_product` (through the registry), so this can never differ —
    # it's load-bearing only for `github:`, where `product` came from
    # `bare_repo` independently of whatever the caller named here.
    payload_product = fields["payload_product"]
    if payload_product is not None and projects.resolve(payload_product) != product:
        return None, refusals.refuse(
            400, "product does not match the product repo_identity maps to",
            product=payload_product)
    if not identity.in_scope(ident, product):
        return None, refusals.not_found()
    return product, None


def _review_record_pr_key(value) -> str | None:
    """Normalize a `pr` value (a payload int or a facts string) to a decimal
    string for comparison, or `None` when it names no PR at all.

    `None`, `""`, and a missing key all mean "no PR". Anything else is
    compared as a decimal integer (`int(str(value))`), which fails closed —
    raising rather than silently treating an unparseable value as absent —
    for whichever caller catches it.
    """
    if value is None or value == "":
        return None
    return str(int(str(value)))


def _review_record_auth(fields: dict, facts: dict | None) -> tuple[str, str | None]:
    """`(auth, auth_reason)` — the Library's own determination, never the
    caller's to assert.

    **`run-verified` requires a pinned, matching commit, a present, matching
    repo, and a matching `pr`** — an empty `review_sha` or an empty `repo`
    fact is `unverified` rather than a free pass, because both used to be
    read as "nothing to compare, so let it through", which let an unpinned
    run with no repo fact mint `run-verified` for any repository and commit
    a caller named. A measurement over every review-class run from 2026-08
    and 2026-09 found 10 runs (of 6,554) with an empty `review_sha` and zero
    with an empty `repo`, the latest unpinned one on 2026-09-07. This table
    is new and its backfill writes only `unverified`, so no record depends on
    the old skip; an unpinned review now needs a pinned re-dispatch to count.

    **`pr` is bound to the run for the same reason `repo` and `review_sha`
    are: it isn't part of `write_review_record`'s uniqueness key
    (`repo_identity`, `identity_key`, `role`, `reviewed_ref`), so a re-post
    of the same run/role/commit can change it freely.** Left unchecked, a
    caller could re-post a genuine `run-verified` review under a different
    `pr` and supersede the row into `run-verified` on a pull request the run
    never reviewed — exactly the shape `GET /review-records/verdicts`,
    reading by `(repository, pull request)` (the index this table was built
    to serve), would trust. That route has no reader outside this
    repository's own tests, and no merge gate reads this table at all;
    Deck's PR ladder reads the full `GET /review-records` instead. The
    comparison is equality via `_review_record_pr_key`, not mere
    non-emptiness: a run with no `pr` verifies a record with no `pr`, and
    nothing else.

    **A `git:` identity is never `run-verified`.** `run_facts` carries a
    bare repo name and no root commit, so there is nothing in the facts to
    bind a `git:<root-sha>` identity to — the check sits right after the
    run-found and private checks, before role/repo/sha/pr, because none of
    those can make a `git:` record trustworthy either. A later facts field
    (a root commit) could lift this.
    """
    if fields["run_id"] is None:
        return store.REVIEW_AUTH_UNVERIFIED, "no run"
    if not facts.get("found"):
        return store.REVIEW_AUTH_UNVERIFIED, "run not found"
    if facts.get("private"):
        return store.REVIEW_AUTH_UNVERIFIED, "private run"
    if fields["scheme"] == "git":
        return (store.REVIEW_AUTH_UNVERIFIED,
                "git identity cannot be verified against a run")
    if facts.get("review_role") != fields["role"] or not facts.get("review_role"):
        return store.REVIEW_AUTH_UNVERIFIED, "role mismatch"
    facts_repo = facts.get("repo") or ""
    if facts_repo == "":
        return store.REVIEW_AUTH_UNVERIFIED, "run repo unknown"
    if facts_repo != fields["bare_repo"]:
        return store.REVIEW_AUTH_UNVERIFIED, "repo mismatch"
    facts_sha = facts.get("review_sha") or ""
    if facts_sha == "":
        return store.REVIEW_AUTH_UNVERIFIED, "run not pinned"
    if facts_sha != fields["reviewed_ref"]:
        return store.REVIEW_AUTH_UNVERIFIED, "review_sha mismatch"
    try:
        facts_pr = _review_record_pr_key(facts.get("pr"))
        fields_pr = _review_record_pr_key(fields["pr"])
    except (TypeError, ValueError):
        return store.REVIEW_AUTH_UNVERIFIED, "pr mismatch"
    if facts_pr != fields_pr:
        return store.REVIEW_AUTH_UNVERIFIED, "pr mismatch"
    return store.REVIEW_AUTH_RUN_VERIFIED, None


def _record_review_signal(ident: identity.Identity, *, record: dict,
                          repo_name: str) -> str | None:
    """The Archives signal every review-record write writes, one per record
    VERSION rather than one per call (issue_01M2HS3Q36RE5F82QXRN4Z0D0K).

    **`dedup_key` carries the version, and that alone is what makes an
    unchanged re-post mint nothing while a supersede mints one.** A repeat
    write that left `review_records.version` unmoved reaches this with the
    SAME `dedup_key` its first write used, so `insert_signal`'s own dedup
    check answers with the existing signal rather than a second one — no
    branch here has to know which case it is.

    **`can_return_existing` is unconditionally `True`, unlike `POST
    /signals`'s own callback.** That route's version guards against a caller
    reading back a row it could not have created or is not scoped to see —
    a real concern for a caller-chosen `dedup_key`. This key is `record_id`
    (a ULID, unguessable) plus `version`, chosen by this function alone
    after the record already exists, so the only way to collide with it is
    to already be a repeat call for the exact same record version — never a
    different caller's row.

    Best-effort, matching `_record_merge_signal`'s reasoning exactly: the
    record write has already committed by the time this runs, so a failure
    here must not undo it or change the route's answer. Returns the signal's
    id on success (new or idempotently returned), or None on a swallowed
    failure — the caller uses this only to decide whether to embed and
    whether to stamp `review_records.signal_id`.
    """
    pr, reviewed_ref = record["pr"], record["reviewed_ref"]
    stream = (f"{repo_name}#{pr}" if pr is not None
             else f"{repo_name}@{reviewed_ref[:12]}")
    row = {
        "id": f"sig_{store.new_ulid()}",
        "source": "library",
        "kind": "review_record",
        "actor": record["role"],
        "ingested_by": ident.actor,
        "acting_human": None,
        "product": record["product"],
        "stream": store.clip(stream, store.MAX_STREAM),
        "body": store.clip(record["prose"], store.MAX_TEXT),
        "refs": json.dumps({
            "record_id": record["id"], "version": record["version"],
            "verdict": record["verdict"], "auth": record["auth"],
            "pr": pr, "reviewed_ref": reviewed_ref}),
        "meta": json.dumps({}),
        "privacy_class": "internal",
        "dedup_key": f"review_record:{record['id']}:v{record['version']}",
        "produced_at": store.produced_now(),
    }
    try:
        status, out = store.insert_signal(row, lambda _existing: True)
    except (store.WritesDisabled, store.ColdTierUnavailable) as exc:
        # **CAUGHT HERE, NOT LEFT TO `cold_tier_unavailable`'s handler** — this
        # function's own contract (above) is that a failure here must not
        # undo the route's already-committed answer, so a `ColdTierUnavailable`
        # raised when a reposted, already-moved review record's `dedup_key`
        # hits the registry is swallowed exactly like `WritesDisabled` always
        # was, not turned into a 503 that would overwrite the 200 `POST
        # /review-records` already decided to send.
        print(f"library: review-record signal NOT recorded for "
              f"{record['id']} v{record['version']}: {exc}", file=sys.stderr)
        return None
    if status not in (200, 201):
        return None
    return out["id"]


@app.post("/review-records")
async def create_review_record(request: Request):
    """Capture one review record — the write route for "All reviews are
    memory" (issue_01M2HS6XNVPG6ZB5RNQDN2TJKN), idempotent on
    (`repo_identity`, `identity_key`, `role`, `reviewed_ref`).

    **`auth` is this route's to decide, never the caller's** — see
    `_review_record_auth`. `run-verified` requires all four: a pinned
    `review_sha` equal to `reviewed_ref`, a present `repo` fact equal to the
    `github:` identity's repo name, a matching `review_role`, and a matching
    `pr`. A `git:` identity is never `run-verified` — `run_facts` has
    nothing to bind a root-commit identity to, so it is always stored
    `unverified`.

    **Product scope comes from `repo_identity` through the crew registry,
    never from the payload** — see `_review_record_product`. `github:`
    resolves through the registry directly; `git:` always resolves through
    the caller's own `product` field (400 when absent), because a `git:`
    record can never be `run-verified` and so its product is only a
    read-scope label, never part of what gates a merge.

    **The Bosun's facts read happens BEFORE any transaction opens**, and a
    facts read that comes back `available: False` refuses the whole call with
    503 and stores nothing — deliberately. A transient outage must not create
    or supersede a record as `unverified`: a re-post during an outage would
    otherwise silently downgrade or drop a `run-verified` APPROVE the outage
    had nothing to do with. `review-post` on the Bosun's side keeps the
    review in hand and retries rather than treating this 503 as a verdict.

    Everything past that point — the insert-or-supersede decision and the
    signal it writes — runs through `store.write_review_record`, which holds
    the one `BEGIN IMMEDIATE` transaction this call opens.

    **A write this route computes `unverified` may never supersede a
    `run-verified` record on the same key** (issue_01M3WBNHB615DTK9VARCRN9S2Y)
    — 409, nothing stored. `run_id` is a plain payload field with no check
    binding it to the caller's own run, so a second post naming the same
    `run_id` as a genuine `run-verified` record, but with a `pr` the run's
    own facts don't match (or a `run_id` the Bosun's register no longer
    resolves), would otherwise degrade a run-verified `REQUEST_CHANGES` to
    an unverified record under a verdict of the poster's choosing — and
    Deck's PR ladder counts `REQUEST_CHANGES` only when run-verified, so the
    block would silently disappear from it. See
    `store.ReviewRecordWouldDowngrade`.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "review_record.create"):
        return refusals.forbidden("review_record.create")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return refusals.refuse(400, "body must be a JSON object")
    if not isinstance(payload, dict):
        return refusals.refuse(400, "body must be a JSON object")

    fields, err = _validate_review_record_payload(payload)
    if err:
        return err

    facts = None
    if fields["run_id"] is not None:
        facts = await run_in_threadpool(runs.run_facts, fields["run_id"])
        if not facts.get("available"):
            # **503, AND NOTHING IS STORED** — see the route's own docstring
            # and step 4 of the epic's write route for why this refusal is
            # deliberate rather than a fallback to `unverified`.
            return refusals.refuse(
                503, "run facts unavailable",
                hint=facts.get("reason") or "the Bosun's run register could "
                                             "not be read")

    product, err = _review_record_product(ident, fields, facts)
    if err:
        return err

    auth, auth_reason = _review_record_auth(fields, facts)

    repo_name = (fields["bare_repo"] if fields["scheme"] == "github"
                else (facts or {}).get("repo") or product)

    row = {
        "repo_identity": fields["repo_identity"], "product": product,
        "pr": fields["pr"], "reviewed_ref": fields["reviewed_ref"],
        "role": fields["role"], "run_id": fields["run_id"],
        "verdict": fields["verdict"], "findings": fields["findings"],
        "prose": fields["prose"], "paths": fields["paths"],
        "doc_ids": fields["doc_ids"], "item_ids": fields["item_ids"],
        "auth": auth, "auth_reason": auth_reason,
        "source_comment_id": fields["source_comment_id"],
    }
    try:
        status, outcome, out = await run_in_threadpool(store.write_review_record, row)
    except store.WritesDisabled as e:
        return refusals.refuse(503, "the Library is not the write path yet",
                               hint=str(e))
    except store.ReviewRecordWouldDowngrade as e:
        # **A write this route itself computed `unverified` may never
        # supersede a `run-verified` record on the same key** — see
        # `store.ReviewRecordWouldDowngrade`. Nothing was stored; the
        # existing record rides along so the caller can see what it did not
        # replace without a second read.
        return refusals.refuse(
            409, "an unverified write may not supersede a run-verified "
                 "review record", hint=str(e), existing=e.existing)

    signal_id = await run_in_threadpool(
        _record_review_signal, ident, record=out, repo_name=repo_name)
    if signal_id is not None and signal_id != out.get("signal_id"):
        await run_in_threadpool(
            store.set_review_record_signal, out["id"], out["version"], signal_id)
        out["signal_id"] = signal_id
        await _embed_after_commit(vectors.VAULT_ARCHIVES, signal_id)

    return JSONResponse(status_code=status, content={**out, "outcome": outcome})


# ---------------------------------------------------------------------------
# The review-record read routes (issue_01M2HS42EA7V4DEXQX1R1PWS9Y), serving
# the three readers the epic (issue_01M2HS6XNVPG6ZB5RNQDN2TJKN) names:
#
# - **`GET /review-records/verdicts`** — built for the Bosun's merge gates
#   (`review-status`, `pr-ready`, `kb merge`), which need every record's
#   `auth` alongside its verdict for one repository and pull request or
#   commit, so a gate that cannot see an `unverified` record can still say
#   why it does not count. Those gates read PR comment stamps authenticated
#   against run facts instead (`agentctl`'s `_authenticated_stamps`), never
#   this table, and Deck's PR ladder reads the full `GET /review-records`.
#   No reader outside this repository's own tests calls this route.
# - **`GET /review-records`** — Deck and Orient, which show a PR's reviews in
#   full, and a session starting work, which retrieves the reviews that
#   touched its paths, documents or work items.
# - **`GET /review-records/{record_id}`** — one record in full, optionally
#   with its superseded versions.
#
# All three are served at protocol version 1 (`protocol.py`) — the only
# version this build serves at all — behind one capability,
# `review_record.read`, granted the same way `signal.read` is: to both
# `agent` (the Bosun, a session) and `system` (Deck, Orient), never on the
# payload.
# ---------------------------------------------------------------------------


def _validate_review_repo_identity(raw: str | None):
    """`(scheme, bare_repo, repo_identity, refusal)` — a read route's own
    wrapper around `_parse_repo_identity`, turning its `reason` into the
    same refusal wording `_validate_review_record_payload` gives the write
    route's body field, so the same string is valid or invalid on both
    sides of the contract by construction, not by two hand-kept copies.
    """
    scheme, bare_repo, repo_identity, reason = _parse_repo_identity(raw)
    if reason == "required":
        return None, None, None, refusals.refuse(400, "repo_identity is required")
    if reason == "too_long":
        return None, None, None, refusals.refuse(
            400, f"repo_identity is longer than "
                 f"{store.MAX_REVIEW_REPO_IDENTITY} characters")
    if reason == "malformed":
        return None, None, None, refusals.refuse(
            400, "repo_identity must be exactly github:<owner>/<repo> or "
                 "git:<40 lowercase hex characters>",
            repo_identity=repo_identity)
    return scheme, bare_repo, repo_identity, None


def _review_record_read_scope(ident: identity.Identity, scheme: str,
                              bare_repo: str | None, alias: str = "") -> tuple[str, list, JSONResponse | None]:
    """The scope clause (or refusal) for a `repo_identity` selector, on the
    two rules the item states: `github:` resolves the product through the
    crew registry and fails closed, the same way `_review_record_product`
    does for the write route; `git:` has no repository name a registry
    entry could key on, so it is scoped the way `path`/`doc_id`/`item_id`
    are — filtered by the caller's own product scope in the query, never
    resolved up front.

    Returns `("", [], refusal)` for an out-of-scope or unresolvable
    `github:` identity — the caller learns nothing more than "no such
    record, or none this identity may see", matching `refusals.not_found`.
    A `git:` identity never refuses here; an out-of-scope caller simply
    matches no row, the same disclosure `_scope_clause` gives every other
    listing in this file.
    """
    if scheme == "github":
        if not projects.available():
            return "", [], refusals.not_found()
        product = projects.resolve(bare_repo)
        if product is None or not identity.in_scope(ident, product):
            return "", [], refusals.not_found()
        q = f"{alias}." if alias else ""
        return f" AND {q}product = ?", [product], None
    return (*_scope_clause(ident, alias), None)


def _review_record_array_match(column: str, value: str) -> tuple[str, list]:
    """A `paths`/`doc_ids`/`item_ids` filter matching a WHOLE stored entry,
    never a substring — `column` is always one of those three literals from
    this file, never a request value, so it is safe in the f-string the same
    way `_customer_wall`'s and `_scope_clause`'s `alias` are.

    **`CASE WHEN json_valid(...) ... ELSE 0 END`, not `json_valid(...) AND
    EXISTS(...)`** — the same guard `_signal_filters`'s `unreferenced_by`
    clause and `list_items`'s `topics` filter use, and for the identical
    reason: `json_each` over an unparseable blob raises rather than skipping
    the row, so one malformed `paths`/`doc_ids`/`item_ids` value anywhere in
    `review_records` would take the whole listing down rather than only its
    own row. `json_each.value` for a string-array element is already
    dequoted, so comparing it with `= ?` matches the entry exactly rather
    than a substring of it.
    """
    return (f" AND (CASE WHEN json_valid({column}) "
            f"AND json_type({column}) = 'array' "
            f"THEN EXISTS (SELECT 1 FROM json_each({column}) "
            f"WHERE json_each.value = ?) ELSE 0 END)"), [value]


@app.get("/review-records/verdicts")
async def review_record_verdicts(request: Request):
    """Verdicts, with their `auth`, for one repository plus a pull request
    or a commit — built for the Bosun's merge gates (`review-status`,
    `pr-ready`, `kb merge`), per the epic that built this table. Those gates
    read PR comment stamps authenticated against run facts instead, never
    this route; Deck's PR ladder reads the full `GET /review-records`. No
    reader outside this repository's own tests calls this route. **Never
    filters `unverified` records out**: a reader that cannot see one cannot
    report why a review does not count toward a merge (the epic's own
    words). No prose, no findings — see `GET /review-records` for the full
    record.

    `repo_identity` is required; exactly one of `pr` or `reviewed_ref` is
    required alongside it — both or neither is 400, since each names a
    different index and a caller has to choose which question it is asking.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "review_record.read"):
        return refusals.forbidden("review_record.read")
    if (err := _reject_unknown_params(
            request, {"repo_identity", "pr", "reviewed_ref"})):
        return err
    scheme, bare_repo, repo_identity, err = _validate_review_repo_identity(
        request.query_params.get("repo_identity"))
    if err:
        return err
    pr_raw = request.query_params.get("pr")
    reviewed_ref = request.query_params.get("reviewed_ref")
    if (pr_raw is None) == (reviewed_ref is None):
        return refusals.refuse(
            400, "pass exactly one of pr or reviewed_ref",
            hint="pr names the (repo_identity, pr) index, reviewed_ref names "
                 "the (repo_identity, reviewed_ref) index — passing both or "
                 "neither leaves it unclear which the caller is asking")
    where, params = "WHERE repo_identity = ?", [repo_identity]
    if pr_raw is not None:
        try:
            pr = int(pr_raw)
        except ValueError:
            return refusals.refuse(400, "pr must be an integer", pr=pr_raw)
        # Hits ix_review_records_repo_pr (repo_identity, pr).
        where += " AND pr = ?"
        params.append(pr)
    else:
        if not _REVIEWED_REF_RE.match(reviewed_ref):
            return refusals.refuse(
                400, "reviewed_ref must be 40 or 64 lowercase hex characters")
        # Hits ix_review_records_repo_ref (repo_identity, reviewed_ref).
        where += " AND reviewed_ref = ?"
        params.append(reviewed_ref)
    scope_where, scope_params, err = _review_record_read_scope(
        ident, scheme, bare_repo)
    if err:
        return err
    where += scope_where
    params += scope_params
    conn = store.connect()
    try:
        rows = conn.execute(
            f"SELECT id, role, verdict, auth, auth_reason, run_id, "
            f"reviewed_ref, pr, version, updated_at FROM review_records "
            f"{where} ORDER BY updated_at DESC", params).fetchall()
    finally:
        conn.close()
    return {"verdicts": [
        {"record_id": r["id"], "role": r["role"], "verdict": r["verdict"],
         "auth": r["auth"], "auth_reason": r["auth_reason"],
         "run_id": r["run_id"], "reviewed_ref": r["reviewed_ref"],
         "pr": r["pr"], "version": r["version"],
         "updated_at": store._iso(r["updated_at"])}
        for r in rows]}


@app.get("/review-records")
async def list_review_records(request: Request):
    """The full record, prose and findings included — Deck's and Orient's
    read, and a session starting work retrieving the reviews that touched
    its paths, documents or work items.

    Exactly one selector: `repo_identity` (with an optional `pr` and/or
    `reviewed_ref` narrowing it further), `path`, `doc_id`, or `item_id`.
    `path`, `doc_id` and `item_id` match a whole stored entry, never a
    substring (`_review_record_array_match`). Paginated with `_page`, newest
    first, `total` and `truncated` in the same shape `GET /items` and the
    Library's other listings use.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "review_record.read"):
        return refusals.forbidden("review_record.read")
    if (err := _reject_unknown_params(request, {
            "limit", "offset", "repo_identity", "path", "doc_id",
            "item_id", "pr", "reviewed_ref"})):
        return err
    limit, offset, err = _page(request)
    if err:
        return err
    qp = request.query_params
    repo_identity_raw = qp.get("repo_identity")
    path, doc_id, item_id = qp.get("path"), qp.get("doc_id"), qp.get("item_id")
    given = sum(s is not None for s in (repo_identity_raw, path, doc_id, item_id))
    if given != 1:
        return refusals.refuse(
            400, "pass exactly one of repo_identity, path, doc_id or item_id",
            hint="repo_identity may be narrowed further with pr and/or "
                 "reviewed_ref, but the selector itself is one of the four")
    where, params = "WHERE 1 = 1", []
    if repo_identity_raw is not None:
        scheme, bare_repo, repo_identity, err = _validate_review_repo_identity(
            repo_identity_raw)
        if err:
            return err
        where += " AND repo_identity = ?"
        params.append(repo_identity)
        if (pr_raw := qp.get("pr")) is not None:
            try:
                pr = int(pr_raw)
            except ValueError:
                return refusals.refuse(400, "pr must be an integer", pr=pr_raw)
            where += " AND pr = ?"
            params.append(pr)
        if (reviewed_ref := qp.get("reviewed_ref")) is not None:
            if not _REVIEWED_REF_RE.match(reviewed_ref):
                return refusals.refuse(
                    400, "reviewed_ref must be 40 or 64 lowercase hex characters")
            where += " AND reviewed_ref = ?"
            params.append(reviewed_ref)
        scope_where, scope_params, err = _review_record_read_scope(
            ident, scheme, bare_repo)
        if err:
            return err
        where += scope_where
        params += scope_params
    else:
        column, value = ("paths", path) if path is not None else (
            ("doc_ids", doc_id) if doc_id is not None else ("item_ids", item_id))
        clause, clause_params = _review_record_array_match(column, value)
        where += clause
        params += clause_params
        scope, scope_params = _scope_clause(ident)
        where += scope
        params += scope_params
    conn = store.connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM review_records {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM review_records {where} ORDER BY updated_at DESC "
            f"LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
    finally:
        conn.close()
    return {"review_records": [store.review_record_dict(r) for r in rows],
            "count": len(rows), "total": total, "offset": offset,
            "truncated": offset + len(rows) < total}


@app.get("/review-records/{record_id}")
async def get_review_record(record_id: str, request: Request):
    """One review record in full. `?include_versions=true` also returns its
    superseded versions from `review_record_versions`, newest version first.
    A record outside the caller's scope is 404, the same as a missing one —
    matching `_readable_signal`'s reasoning, a 403 here would confirm the
    record exists to an identity that may not see it.
    """
    ident, err = _identify(request)
    if err:
        return err
    if not identity.authorized(ident, "review_record.read"):
        return refusals.forbidden("review_record.read")
    if (err := _reject_unknown_params(request, {"include_versions"})):
        return err
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT * FROM review_records WHERE id = ?", [record_id]).fetchone()
    finally:
        conn.close()
    if row is None or not identity.in_scope(ident, row["product"]):
        return refusals.not_found()
    body = store.review_record_dict(row)
    if request.query_params.get("include_versions") == "true":
        conn = store.connect()
        try:
            versions = conn.execute(
                "SELECT * FROM review_record_versions WHERE record_id = ? "
                "ORDER BY version DESC", [record_id]).fetchall()
        finally:
            conn.close()
        body["versions"] = [store.review_record_version_dict(r) for r in versions]
    return body
