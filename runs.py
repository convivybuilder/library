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
import threading
import time
from pathlib import Path

# The Bosun's register, over the same socket shape `bin/session-view` already
# uses. Overridable so the tests can point at one they control.
SOCKET_PATH = Path(os.environ.get(
    "LIBRARY_RUN_REGISTER_SOCKET",
    str(Path.home() / ".local" / "run" / "run-register-api.sock")))

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

# **A CEILING ON THE BODY, because the read loop would otherwise accept whatever
# arrives** (reviewer, PR #51). The register's real answers are a few kilobytes;
# a hundred is four orders of magnitude of headroom and still bounds a Bosun
# that has gone wrong. Without it a large body is read fully into memory, and —
# because the module lock is held across the fetch — every other caller waits
# for the whole transfer.
MAX_BODY_BYTES = 100_000

_lock = threading.Lock()
_cache: tuple[float, dict] | None = None


def _fetch() -> dict:
    """One read of the register. Never raises: every failure is an unavailable
    answer with a reason, because absence is a normal state here."""
    if not SOCKET_PATH.exists():
        return {"available": False, "runs": [],
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
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(TIMEOUT_SECONDS)
    try:
        conn.connect(str(SOCKET_PATH))
        conn.sendall(b"GET /runs HTTP/1.1\r\nHost: localhost\r\n"
                     b"Connection: close\r\n\r\n")
        chunks = []
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"available": False, "runs": [],
                        "reason": f"the run register did not finish answering "
                                  f"within {TIMEOUT_SECONDS}s"}
            # Re-armed every pass, so the LAST recv can never wait past the
            # deadline either.
            conn.settimeout(remaining)
            chunk = conn.recv(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                return {"available": False, "runs": [],
                        "reason": f"the run register's answer is larger than "
                                  f"{MAX_BODY_BYTES} bytes"}
            chunks.append(chunk)
        raw = b"".join(chunks)
    except (OSError, socket.timeout) as e:
        return {"available": False, "runs": [],
                "reason": f"the run register did not answer ({e})"}
    finally:
        conn.close()

    head, _, body = raw.partition(b"\r\n\r\n")
    status = head.split(b"\r\n")[0] if head else b""
    if b" 200 " not in status:
        return {"available": False, "runs": [],
                "reason": f"the run register answered {status.decode('utf-8', 'replace')!r}"}
    try:
        payload = json.loads(body)
    # **`RecursionError` TOO, NOT ONLY `ValueError`** (security-reviewer,
    # PR #51). A deeply nested body raises `RecursionError` from `json.loads`,
    # which is not a `ValueError` and would have escaped as a 500 — breaking
    # this function's own "never raises" contract for exactly the untrusted
    # shape it exists to absorb.
    except (ValueError, RecursionError) as e:
        return {"available": False, "runs": [],
                "reason": f"the run register's answer is not JSON ({e})"}
    if not isinstance(payload, dict) or not isinstance(payload.get("runs"), list):
        # SHAPE, NOT ONLY PARSEABILITY — the same guard the Reference Room's
        # JSON columns take. A payload that parses and is not the agreed shape
        # would otherwise reach the allowlist loop below as an AttributeError.
        return {"available": False, "runs": [],
                "reason": "the run register's answer is not the agreed shape"}

    runs = []
    for row in payload["runs"]:
        if not isinstance(row, dict):
            continue
        runs.append({field: row.get(field) for field in RUN_FIELDS})
    return {"available": True, "runs": runs,
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
    global _cache
    with _lock:
        _cache = None


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
# `{"found": true, "private": true}` and nothing else ever reaches a caller.
RUN_FACTS_FIELDS = ("agent", "repo", "review_sha", "pr", "review_role")


def run_facts(run_id: str) -> dict:
    """One run's facts for the write route's `auth` decision. Never raises.

    A malformed `run_id` is refused without touching the socket — it cannot
    name a real run, so there is nothing to ask the Bosun and no reason to
    spend a round trip finding that out. It answers the same as an id the
    Bosun does not recognise: `{"available": True, "found": False}`.

    Everything past that point is `_fetch`'s shape, reproduced rather than
    shared: a distinct GET target, no cache, and its own deadline — sharing
    `_fetch` would mean a run-facts read taking the module lock `snapshot()`
    holds across its own fetch, which is a real contention two independent
    callers should not have.

    `{"available": False, "reason": ...}` covers every failure the socket
    can produce: unreachable, timed out, a non-200, or a body that is not the
    agreed JSON shape. The write route reads `available` first and refuses
    503 on `False` rather than treating it as `unverified` — a transient
    outage must not silently downgrade or supersede a record that a working
    read would have verified.
    """
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        return {"available": True, "found": False}
    if not SOCKET_PATH.exists():
        return {"available": False,
                "reason": f"no run register at {SOCKET_PATH}"}
    deadline = time.monotonic() + TIMEOUT_SECONDS
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(TIMEOUT_SECONDS)
    try:
        conn.connect(str(SOCKET_PATH))
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
        return {"available": True, "found": True, "private": True}
    return {"available": True, "found": True,
            **{field: payload.get(field) for field in RUN_FACTS_FIELDS}}
