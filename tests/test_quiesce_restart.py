"""platform/decisions/0059's quiesce-and-restart: once a deploy area updates,
this process must stop taking new work, wait for a REAL drain, and only then
exit so launchd's KeepAlive restarts it on the new code.

**The one case that matters most**: a stuck request must BLOCK the restart.
A healthy idle process and a wedged one both read zero in-flight requests, so
the sequencing — refuse new work FIRST, confirm the drain SECOND — is what
makes a later zero mean "actually quiet" instead of "quiet this instant,
possibly by luck". These tests hold that ordering, the refusal itself, and
the "never arm on an unresolved commit" guard, against `_restart_watcher` and
`refuse_during_quiesce` directly rather than through a real subprocess
restart, which nothing in this suite is set up to observe.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import protocol
import store

AGENT = "agent-token"
GOOD_SHA = "a" * 40
OTHER_SHA = "b" * 40


def _auth():
    return {"Authorization": f"Bearer {AGENT}", protocol.HEADER: str(protocol.CURRENT)}


@pytest.fixture(autouse=True)
def _reset_quiesce_state():
    libapp._QUIESCING = False
    libapp._IN_FLIGHT = 0
    yield
    libapp._QUIESCING = False
    libapp._IN_FLIGHT = 0


@pytest.fixture
def client(tmp_path, monkeypatch, vector_index):
    db = tmp_path / "control.db"
    vector_index(db)
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({AGENT: {"name": "an-agent", "products": ["*"]}}))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    return TestClient(libapp.app)


# --- refuse_during_quiesce middleware ----------------------------------------


def test_ordinary_requests_are_not_refused(client):
    r = client.get("/whoami", headers=_auth())
    assert r.status_code != 503


def test_new_work_is_refused_once_quiescing(client, monkeypatch):
    monkeypatch.setattr(libapp, "_QUIESCING", True)
    r = client.get("/whoami", headers=_auth())
    assert r.status_code == 503
    assert "quiescing" in r.json()["error"]


def test_health_is_exempt_from_the_refusal(client, monkeypatch):
    monkeypatch.setattr(libapp, "_QUIESCING", True)
    r = client.get("/health")
    assert r.status_code == 200


def test_in_flight_increments_and_decrements_across_a_request(client):
    assert libapp._IN_FLIGHT == 0
    r = client.get("/whoami", headers=_auth())
    assert r.status_code in (200, 401, 403)
    assert libapp._IN_FLIGHT == 0, "must return to zero once the request completes"


def test_a_refused_request_is_never_counted_in_flight(client, monkeypatch):
    """The false reading the drain logic exists to rule out: a request
    refused outright must not appear in `_IN_FLIGHT` even momentarily,
    since it was never admitted."""
    monkeypatch.setattr(libapp, "_QUIESCING", True)
    client.get("/whoami", headers=_auth())
    assert libapp._IN_FLIGHT == 0


def test_health_requests_are_never_counted_in_flight(client):
    """`/health` is a probe, not the work the drain waits on — it must not
    move the counter the watcher is waiting to see hit zero."""
    client.get("/health")
    assert libapp._IN_FLIGHT == 0


def test_in_flight_decrements_even_if_the_handler_raises():
    """A crashing handler must not leave the counter stuck above zero
    forever — that would be the middleware itself becoming the "stuck
    request" the drain logic is built to detect from the OUTSIDE. Driven
    directly against the middleware function, with a fake request and a
    `call_next` that raises, rather than through real routing — simpler and
    more direct than provoking a real handler into throwing."""
    class _URL:
        path = "/not-health"

    class _Request:
        url = _URL()

    async def call_next_that_raises(_request):
        raise RuntimeError("handler exploded")

    async def _drive():
        with pytest.raises(RuntimeError):
            await libapp.refuse_during_quiesce(_Request(), call_next_that_raises)
        assert libapp._IN_FLIGHT == 0
    asyncio.run(_drive())


# --- _restart_watcher: arming ------------------------------------------------


async def _run_watcher_until_quiescing(monkeypatch, staged, running):
    """Drive `_restart_watcher` far enough to observe whether it arms,
    without sleeping for real and without letting it fall through into the
    drain-and-kill half (that half is tested separately, with the process
    kill call itself stubbed out).

    **Patched sleep must still actually yield to the event loop.** A
    zero-duration coroutine with no `await` inside it never suspends, so
    `asyncio.create_task(...)` would sit scheduled-but-never-run for the
    whole loop below — the real `asyncio.sleep(0)` is special-cased by
    asyncio to reschedule via the loop rather than return synchronously, so
    it is what this delegates to, captured before the module-level patch
    below replaces `asyncio.sleep` itself.
    """
    real_sleep = asyncio.sleep
    monkeypatch.setattr(libapp, "RUNNING_COMMIT", running)
    monkeypatch.setattr(libapp, "_staged_commit", lambda: staged)
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: real_sleep(0))

    killed = []
    monkeypatch.setattr(libapp.os, "kill", lambda *a: killed.append(a))

    task = asyncio.create_task(libapp._restart_watcher())
    for _ in range(50):
        await real_sleep(0)
        if libapp._QUIESCING:
            break
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return killed


def test_watcher_arms_when_staged_differs_from_running(monkeypatch):
    """Arming itself — the comparison, not what happens after. With nothing
    in flight (the default here), the watcher legitimately falls straight
    through to self-terminating in the same breath, which is its own
    dedicated case below (`test_watcher_exits_immediately_...`); this one is
    only about whether the comparison fires."""
    asyncio.run(
        _run_watcher_until_quiescing(monkeypatch, staged=OTHER_SHA, running=GOOD_SHA))
    assert libapp._QUIESCING is True


def test_watcher_does_not_arm_when_staged_matches_running(monkeypatch):
    async def _drive():
        monkeypatch.setattr(libapp, "RUNNING_COMMIT", GOOD_SHA)
        monkeypatch.setattr(libapp, "_staged_commit", lambda: GOOD_SHA)
        real_sleep = asyncio.sleep
        calls = {"n": 0}

        async def counting_sleep(_seconds):
            calls["n"] += 1
            if calls["n"] > 3:
                raise asyncio.CancelledError()
            await real_sleep(0)
        monkeypatch.setattr(asyncio, "sleep", counting_sleep)

        task = asyncio.create_task(libapp._restart_watcher())
        with pytest.raises(asyncio.CancelledError):
            await task
        assert libapp._QUIESCING is False
    asyncio.run(_drive())


def test_watcher_does_not_arm_when_staged_is_unresolved(monkeypatch):
    """None on the staged side must not authorize a restart — "cannot tell"
    is not "differs"."""
    async def _drive():
        monkeypatch.setattr(libapp, "RUNNING_COMMIT", GOOD_SHA)
        monkeypatch.setattr(libapp, "_staged_commit", lambda: None)
        real_sleep = asyncio.sleep
        calls = {"n": 0}

        async def counting_sleep(_seconds):
            calls["n"] += 1
            if calls["n"] > 3:
                raise asyncio.CancelledError()
            await real_sleep(0)
        monkeypatch.setattr(asyncio, "sleep", counting_sleep)

        task = asyncio.create_task(libapp._restart_watcher())
        with pytest.raises(asyncio.CancelledError):
            await task
        assert libapp._QUIESCING is False
    asyncio.run(_drive())


def test_watcher_does_not_arm_when_running_is_unresolved(monkeypatch):
    """Symmetric case: RUNNING_COMMIT unresolved must not arm either, even
    against a well-formed staged value."""
    async def _drive():
        monkeypatch.setattr(libapp, "RUNNING_COMMIT", None)
        monkeypatch.setattr(libapp, "_staged_commit", lambda: OTHER_SHA)
        real_sleep = asyncio.sleep
        calls = {"n": 0}

        async def counting_sleep(_seconds):
            calls["n"] += 1
            if calls["n"] > 3:
                raise asyncio.CancelledError()
            await real_sleep(0)
        monkeypatch.setattr(asyncio, "sleep", counting_sleep)

        task = asyncio.create_task(libapp._restart_watcher())
        with pytest.raises(asyncio.CancelledError):
            await task
        assert libapp._QUIESCING is False
    asyncio.run(_drive())


# --- _restart_watcher: the drain, and the case that matters most ------------


def test_watcher_exits_immediately_when_armed_with_zero_in_flight(monkeypatch):
    """Once quiescing with nothing outstanding, the watcher proceeds straight
    to self-terminating — no unnecessary wait."""
    async def _drive():
        libapp._QUIESCING = True
        libapp._IN_FLIGHT = 0
        killed = []
        monkeypatch.setattr(libapp.os, "kill", lambda *a: killed.append(a))
        # Sleep must not be reached at all on this path; a call would hang
        # the test under the real asyncio.sleep, so make that failure loud.
        monkeypatch.setattr(
            asyncio, "sleep",
            lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("must not sleep with zero already in flight")))

        await asyncio.wait_for(libapp._restart_watcher(), timeout=1.0)
        assert killed, "must self-signal once quiet is confirmed"
        assert killed[0][1] == libapp.signal.SIGTERM
    asyncio.run(_drive())


def test_watcher_waits_for_in_flight_to_reach_zero_before_restarting(monkeypatch):
    """The core ordering property: armed while work is outstanding, the
    watcher must not restart until that work actually finishes — not
    merely until some poll happens to read zero."""
    async def _drive():
        libapp._QUIESCING = True
        libapp._IN_FLIGHT = 2
        killed = []
        monkeypatch.setattr(libapp.os, "kill", lambda *a: killed.append(a))

        drains = {"n": 0}
        real_sleep = asyncio.sleep

        async def draining_sleep(_seconds):
            drains["n"] += 1
            if drains["n"] == 1:
                assert not killed, "must not have restarted before draining"
                libapp._IN_FLIGHT = 1
            elif drains["n"] == 2:
                assert not killed, "must not have restarted before fully drained"
                libapp._IN_FLIGHT = 0
            await real_sleep(0)
        monkeypatch.setattr(asyncio, "sleep", draining_sleep)

        await asyncio.wait_for(libapp._restart_watcher(), timeout=1.0)
        assert drains["n"] >= 2, "must actually have polled the drain more than once"
        assert killed, "must restart once the drain genuinely reaches zero"
    asyncio.run(_drive())


def test_a_stuck_request_blocks_the_restart_until_the_drain_deadline(monkeypatch):
    """THE CASE THAT MATTERS MOST, CORRECTED (the operator, PR #70 round 1). `_IN_FLIGHT`
    that never reaches zero (a wedged request, indistinguishable here from a
    slow one — the watcher is not supposed to be able to tell them apart)
    must not let the watcher proceed to self-terminate BEFORE the drain
    deadline — quiescing refuses the whole fleet, so waiting a bounded time
    still protects a genuinely slow request while capping the lockout a
    permanently wedged one causes."""
    async def _drive():
        libapp._QUIESCING = True
        libapp._IN_FLIGHT = 1  # never goes back to 0 — the stuck request
        killed = []
        exited = []
        monkeypatch.setattr(libapp.os, "kill", lambda *a: killed.append(a))
        monkeypatch.setattr(libapp.os, "_exit", lambda code: exited.append(code))
        monkeypatch.setattr(libapp, "_DRAIN_POLL_SECONDS", 0.001)
        monkeypatch.setattr(libapp, "_DRAIN_DEADLINE_SECONDS", 10.0)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(libapp._restart_watcher(), timeout=0.2)
        assert not killed, "a stuck request must have blocked the restart"
        assert not exited, "the drain deadline had not been reached yet"
    asyncio.run(_drive())


def test_a_stuck_request_force_exits_once_the_drain_deadline_passes(monkeypatch):
    """Past `_DRAIN_DEADLINE_SECONDS`, a request still stuck no longer blocks
    the restart — the process force-exits and drops it, trading an
    unbounded fleet-wide lockout for a bounded one and a dropped request
    `KeepAlive` already treats as recoverable."""
    async def _drive():
        libapp._QUIESCING = True
        libapp._IN_FLIGHT = 1  # never goes back to 0 — the stuck request
        killed = []
        exited = []
        monkeypatch.setattr(libapp.os, "kill", lambda *a: killed.append(a))
        monkeypatch.setattr(libapp.os, "_exit", lambda code: exited.append(code))
        monkeypatch.setattr(libapp, "_DRAIN_POLL_SECONDS", 0.001)
        monkeypatch.setattr(libapp, "_DRAIN_DEADLINE_SECONDS", 0.02)

        await asyncio.wait_for(libapp._restart_watcher(), timeout=1.0)
        assert not killed, "must force-exit rather than SIGTERM once wedged past the deadline"
        assert exited == [1]
    asyncio.run(_drive())


def test_drain_deadline_logs_how_many_requests_were_still_in_flight(monkeypatch, capsys):
    """The deadline firing is the signal that something is wedged, and it
    must not be silent — the log must name how many requests were still
    outstanding when it fired."""
    async def _drive():
        libapp._QUIESCING = True
        libapp._IN_FLIGHT = 3
        monkeypatch.setattr(libapp.os, "kill", lambda *a: None)
        monkeypatch.setattr(libapp.os, "_exit", lambda code: None)
        monkeypatch.setattr(libapp, "_DRAIN_POLL_SECONDS", 0.001)
        monkeypatch.setattr(libapp, "_DRAIN_DEADLINE_SECONDS", 0.02)
        await asyncio.wait_for(libapp._restart_watcher(), timeout=1.0)
    asyncio.run(_drive())
    err = capsys.readouterr().err
    assert "DEADLINE" in err.upper()
    assert "3" in err


def test_drain_within_the_deadline_still_restarts_normally(monkeypatch):
    """A request that finishes before the deadline takes the ordinary SIGTERM
    path, not the force-exit one — the deadline only fires once it is
    actually reached."""
    async def _drive():
        libapp._QUIESCING = True
        libapp._IN_FLIGHT = 1
        killed = []
        exited = []
        monkeypatch.setattr(libapp.os, "kill", lambda *a: killed.append(a))
        monkeypatch.setattr(libapp.os, "_exit", lambda code: exited.append(code))
        monkeypatch.setattr(libapp, "_DRAIN_DEADLINE_SECONDS", 10.0)

        real_sleep = asyncio.sleep

        async def draining_sleep(_seconds):
            libapp._IN_FLIGHT = 0
            await real_sleep(0)
        monkeypatch.setattr(asyncio, "sleep", draining_sleep)

        await asyncio.wait_for(libapp._restart_watcher(), timeout=1.0)
        assert not exited, "must not force-exit when the drain finished in time"
        assert killed and killed[0][1] == libapp.signal.SIGTERM
    asyncio.run(_drive())
