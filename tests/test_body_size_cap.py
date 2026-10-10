"""The request body cap, which runs before anything reads the body.

**EVERY TEST HERE HAS TO DISTINGUISH "REFUSED" FROM "REFUSED FOR THE RIGHT
REASON".** The Library already refuses an oversized `content` with a 400 after
parsing it, so a test that asserts only "this request failed" passes identically
against a build with no cap at all. What separates them is the status (413, not
400), and — for the case that matters most — that the body was never fully read.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import protocol
import store

TOKEN = "agent-token-vivy"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """**A REAL TOKEN, because the counter only runs when something reads the
    body.** `create_signal` checks identity before it calls `await
    request.json()`, so an unauthenticated oversized request is refused 401 with
    the body never pulled off the wire — correct, and it means an unauthenticated
    caller cannot exercise the counting path at all. Testing it needs a caller
    that gets far enough to read."""
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps(
        {TOKEN: {"name": "vivy-agent", "products": ["vivy"]}}))
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "control.db")
    return TestClient(libapp.app)


def _h(token=TOKEN):
    return {protocol.HEADER: str(protocol.CURRENT),
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"}


def test_the_escape_expansion_ceiling_holds_across_all_of_unicode():
    """**THE DERIVATION ITSELF, RECOMPUTED RATHER THAN SAMPLED — and sampling is
    exactly what let two wrong multipliers ship green** (reviewer and
    security-reviewer, PR #66, in two consecutive rounds).

    Round 1 assumed 1.4x, round 2 assumed 2x from `"`, `\\` and newline. Both
    were broken by a character class nobody had listed: `json.dumps` defaults to
    `ensure_ascii=True`, which escapes a 2-byte Cyrillic or Hebrew character to
    six bytes and a 4-byte emoji to twelve. A test naming characters can only
    ever check the ones its author thought of, which is the same shape as a
    guard that passes because nobody probed the case it misses.

    So this checks EVERY code point under both encoder settings and asserts the
    constant is not below what it finds. It takes about a second, and it is the
    test that ends this class of finding: if a future Python escapes anything
    more aggressively, this fails rather than a caller's legitimate body being
    refused in production."""
    worst, at = 0, None
    for cp in range(0x110000):
        if 0xD800 <= cp <= 0xDFFF:      # surrogates are not encodable alone
            continue
        ch = chr(cp)
        raw = len(ch.encode())
        for ensure_ascii in (True, False):
            escaped = len(json.dumps(ch, ensure_ascii=ensure_ascii).encode()) - 2
            if escaped / raw > worst:
                worst, at = escaped / raw, (cp, ensure_ascii)
    assert worst <= store.JSON_MAX_ESCAPE_EXPANSION, (
        f"U+{at[0]:04X} expands {worst:.1f}x under ensure_ascii={at[1]}, past "
        f"the {store.JSON_MAX_ESCAPE_EXPANSION}x the envelope is sized for — so "
        f"a legal `content` of it would be refused")
    assert worst == store.JSON_MAX_ESCAPE_EXPANSION, (
        f"the ceiling is {store.JSON_MAX_ESCAPE_EXPANSION}x and nothing reaches "
        f"past {worst:.1f}x, so the envelope is larger than it needs to be")


@pytest.mark.parametrize("name,char", [
    ("newline", "\n"), ("quote", '"'), ("backslash", "\\"), ("ascii", "a"),
    ("control", "\x01"), ("cyrillic", "\u0430"), ("hebrew", "\u05d0"),
    ("accented latin", "\u00e9"), ("cjk", "\u6f22"), ("emoji", "\U0001f600"),
])
@pytest.mark.parametrize("ensure_ascii", [True, False])
def test_the_envelope_holds_the_largest_body_a_route_will_accept(
        name, char, ensure_ascii):
    """**EVERY CHARACTER CLASS, UNDER BOTH ENCODER SETTINGS.** `ensure_ascii` is
    parametrized because it is where round 2's gap lived: `json.dumps` and
    `requests` default to True and escape all non-ASCII, while httpx — what this
    suite's own TestClient uses — passes False. A test that only went through the
    TestClient structurally could not reach the path a `requests` caller hits
    every day.

    `content` is at exactly `MAX_BLOB_BYTES`, the largest the blob route takes."""
    content = char * (store.MAX_BLOB_BYTES // len(char.encode()))
    assert len(content.encode()) <= store.MAX_BLOB_BYTES
    envelope = json.dumps({"source": "s", "kind": "k", "product": "vivy",
                           "body": "b", "content": content},
                          ensure_ascii=ensure_ascii).encode()
    assert len(envelope) <= store.MAX_REQUEST_BYTES, (
        f"{name} content at MAX_BLOB_BYTES encodes to {len(envelope):,} bytes "
        f"under ensure_ascii={ensure_ascii}, which the envelope refuses — so "
        f"the cap rejects a body the blob route would have accepted")


def test_the_worst_case_body_is_actually_ACCEPTED_over_the_wire(client):
    """**THE ENVELOPE MEASURED IS NOT THE ENVELOPE SENT, so this posts one**
    (reviewer, PR #66). Sent as pre-encoded bytes with `ensure_ascii=True`
    rather than through `json=`, because httpx would otherwise re-encode it with
    `ensure_ascii=False` and the request would never carry the escaping this is
    about — the blind spot that let round 2 ship green.

    Cyrillic at exactly `MAX_BLOB_BYTES`: 20 MB of UTF-8, 60 MB on the wire."""
    content = "\u0430" * (store.MAX_BLOB_BYTES // 2)
    body = json.dumps({"source": "probe", "kind": "note", "product": "vivy",
                       "content": content}, ensure_ascii=True).encode()
    assert len(body) > store.MAX_BLOB_BYTES * 2, "not exercising the 3x path"
    r = client.post("/signals", headers=_h(), content=body)
    assert r.status_code != 413, (
        f"a legal Cyrillic `content` at MAX_BLOB_BYTES was refused by the "
        f"envelope: {r.json()}")


def test_the_envelope_is_above_the_other_field_caps():
    assert store.MAX_REQUEST_BYTES > store.MAX_ITEM_TEXT
    assert store.MAX_REQUEST_BYTES > store.MAX_TEXT


def test_the_cap_is_the_outermost_middleware(client):
    """Starlette applies `add_middleware` in reverse, so the outermost is the
    LAST registered and sits FIRST in `user_middleware`. Asserted by position
    rather than by reading the file, because the invariant is about what wraps
    what — a middleware added below the registration call would take this slot
    and read an unbounded body itself."""
    outermost = libapp.app.user_middleware[0]
    assert outermost.cls is libapp.RequestBodySizeLimit, (
        f"{outermost.cls.__name__} wraps outside the body cap, so it reads a "
        f"body nothing has bounded")


def test_the_fast_path_refuses_an_unauthenticated_caller_too(client):
    """**THE CAP SITS OUTSIDE AUTHENTICATION, and that ordering is the point.**
    Buffering 28 MB from a caller the Library will refuse anyway is the whole
    cost this avoids, so the header check must not wait for a token to be
    resolved first."""
    over = store.MAX_REQUEST_BYTES + 1
    r = client.post("/signals",
                    headers={**_h("not-a-real-token"),
                             "Content-Length": str(over)},
                    content=b"")
    assert r.status_code == 413, (
        "an unauthenticated oversized request reached authentication, so the "
        "cap runs inside the check it is supposed to precede")


def test_a_declared_length_over_the_cap_is_refused_before_the_body_is_read(client):
    """The fast path: refused on the header, with no body sent at all."""
    over = store.MAX_REQUEST_BYTES + 1
    r = client.post("/signals", headers={**_h(), "Content-Length": str(over)},
                    content=b"")
    assert r.status_code == 413
    body = r.json()
    assert body["limit"] == store.MAX_REQUEST_BYTES
    assert "larger than" in body["error"]
    # **`declared`, NOT `received`.** Nothing was received on this path — the
    # header was read and the request refused before a body byte arrived — so
    # reporting it as received would tell the caller the Library buffered what
    # it refused, which is the opposite of what this does.
    assert body["declared"] == over and "received" not in body


def test_an_oversized_body_with_no_declared_length_is_still_refused(client):
    """**THE BYPASS A HEADER-ONLY CAP LEAVES OPEN, and the reason the counter
    exists.** `Content-Length` is the caller's claim about its own body; omitting
    it is free. Sent as a generator so httpx uses chunked encoding and declares
    no length, this request has nothing for the fast path to check.

    The chunk size is over the cap on its own, so the refusal fires on the first
    one rather than after streaming 28 MB through the test."""
    def chunks():
        yield b"x" * (store.MAX_REQUEST_BYTES + 1)

    r = client.post("/signals", headers=_h(), content=chunks())
    assert r.status_code == 413, (
        "an undeclared oversized body was accepted, so the cap can be bypassed "
        "by omitting one header")


def test_a_body_larger_than_its_declared_length_is_refused_by_the_counter(client):
    """A length that passes the fast path and a body that does not match it. The
    header is a claim; the counter is the check."""
    def chunks():
        yield b"x" * (store.MAX_REQUEST_BYTES + 1)

    r = client.post("/signals", headers={**_h(), "Content-Length": "10"},
                    content=chunks())
    assert r.status_code == 413
    # The counter fired, so these bytes really were read: `received`, not
    # `declared`.
    assert r.json()["received"] > store.MAX_REQUEST_BYTES
    assert "declared" not in r.json()


def test_a_malformed_declared_length_is_refused_rather_than_ignored(client):
    """Skipping an unparseable header would mean a caller opts out of the fast
    path by corrupting it."""
    r = client.post("/signals", headers={**_h(), "Content-Length": "ten"},
                    content=b"{}")
    assert r.status_code == 400
    assert "Content-Length" in r.json()["error"]


def test_a_negative_declared_length_is_refused(client):
    """`int(b"-1")` parses and passes any `> max` comparison while describing a
    body that cannot exist."""
    r = client.post("/signals", headers={**_h(), "Content-Length": "-1"},
                    content=b"{}")
    assert r.status_code == 400
    assert r.json()["declared"] == -1


def test_an_ordinary_request_passes_through_untouched(client):
    """**THE CONTROL, and without it every assertion above passes against a
    middleware that refuses everything.** This body is well under the cap, so it
    reaches the route and is refused for its own reasons — an unknown token —
    rather than by the cap."""
    r = client.post("/signals", headers=_h("not-a-real-token"),
                    json={"source": "test"})
    assert r.status_code != 413
    assert r.status_code == 401, "reached the route and was refused on identity"


def test_a_get_with_no_body_is_untouched(client):
    """A liveness probe must not pay for this."""
    r = client.get("/health")
    assert r.status_code == 200
