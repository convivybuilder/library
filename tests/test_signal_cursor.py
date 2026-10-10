"""Paging the Archives to the end, which offset paging cannot do.

`MAX_OFFSET` refuses an offset past 10,000 — rightly, since a clamped offset
silently returns a different page than the one asked for — and the Archives hold
19,000 rows. Orient's `library_client` pages to completion and stops with an
`HTTPError` partway, so its signals page has been serving a stale static fallback
rather than live data (`issue_01M06Y897250HGZHFQH2VP1YZS`).

A keyset walk has no depth limit, because it never counts the rows it has passed.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import protocol
import store

TOKEN = "t-reader"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE signals (
            id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
            ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
            privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
            embedded_at TEXT, dedup_key TEXT, acting_human TEXT);
    """)
    conn.commit()
    conn.close()
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({TOKEN: {"name": "reader", "products": ["*"]}}))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    return TestClient(libapp.app)


def _h():
    return {"Authorization": f"Bearer {TOKEN}",
            protocol.HEADER: str(protocol.CURRENT)}


def seed(n, *, same_instant=False, product="keel"):
    """`n` signals, newest last. `same_instant` puts them all in one second,
    which a capture run really does."""
    conn = sqlite3.connect(store.DB_PATH)
    for i in range(n):
        at = ("2026-08-01 12:00:00.000000" if same_instant
              else f"2026-08-01 12:00:{i // 60:02d}.{i % 60:06d}")
        conn.execute(
            "INSERT INTO signals (id, source, kind, actor, product, body, "
            "privacy_class, ingested_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"sig_{i:05d}", "test", "note", "someone", product,
             f"body {i}", "internal", at))
    conn.commit()
    conn.close()


def walk(client, query="", limit=50):
    """Page to completion through the cursor, returning every id in order."""
    seen, cursor, pages = [], None, 0
    while True:
        q = f"/signals?limit={limit}{query}" + (f"&cursor={cursor}" if cursor else "")
        body = client.get(q, headers=_h()).json()
        seen += [s["id"] for s in body["signals"]]
        pages += 1
        cursor = body["next_cursor"]
        if not cursor:
            return seen, pages
        assert pages < 500, "the walk did not terminate"


# --- the walk ---------------------------------------------------------------------

def test_a_cursor_walk_reaches_every_row(client):
    seed(120)
    ids, pages = walk(client, limit=25)
    assert len(ids) == 120
    assert len(set(ids)) == 120, "the walk repeated a row"
    assert pages == 5


def test_the_walk_is_in_the_listing_s_own_order(client):
    seed(60)
    ids, _ = walk(client, limit=10)
    assert ids == sorted(ids, reverse=True), "newest first, as the listing serves"


def test_the_walk_passes_the_offset_ceiling(client, monkeypatch):
    """**The failure this exists for.** The real table is 19,000 rows against a
    10,000 offset cap; the cap is lowered here rather than seeding 19,000 rows,
    because what is being tested is that the walk never consults an offset at
    all."""
    monkeypatch.setattr(libapp, "MAX_OFFSET", 20)
    seed(120)
    refused = client.get("/signals?limit=25&offset=25", headers=_h())
    assert refused.status_code == 400, "the offset ceiling is still enforced"
    ids, _ = walk(client, limit=25)
    assert len(ids) == 120, "the cursor walk is bounded by the offset cap"


def test_rows_sharing_an_instant_are_not_dropped(client):
    """**The id half of the key is load-bearing.** A capture run writes several
    signals inside one second, so without it a page boundary landing mid-tie
    would drop every tied row but one."""
    seed(40, same_instant=True)
    ids, _ = walk(client, limit=7)
    assert len(ids) == 40 and len(set(ids)) == 40


def test_a_row_written_mid_walk_does_not_shift_the_page(client):
    """Offset paging is wrong under concurrent writes — a signal captured while a
    client pages shifts every later offset by one, so the client re-reads one row
    and skips another. A cursor names the last row seen, so an arrival simply is
    not in the walk."""
    seed(30)
    first = client.get("/signals?limit=10", headers=_h()).json()
    seen = [s["id"] for s in first["signals"]]
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute("INSERT INTO signals (id, source, kind, actor, product, body, "
                 "privacy_class, ingested_at) VALUES "
                 "('sig_99999','test','note','a','keel','late','internal',"
                 "'2026-08-09 00:00:00.000000')")
    conn.commit()
    conn.close()
    cursor = first["next_cursor"]
    while cursor:
        body = client.get(f"/signals?limit=10&cursor={cursor}", headers=_h()).json()
        seen += [s["id"] for s in body["signals"]]
        cursor = body["next_cursor"]
    assert len(seen) == len(set(seen)), "a row was read twice"
    assert "sig_99999" not in seen, "a row written after the walk began joined it"


# --- what the response carries ----------------------------------------------------

def test_the_last_page_ends_the_walk(client):
    seed(20)
    body = client.get("/signals?limit=50", headers=_h()).json()
    assert body["truncated"] is False
    assert body["next_cursor"] is None, (
        "a non-null cursor at the end is an infinite loop in every client that "
        "trusts it")


def test_an_offset_page_still_offers_the_cursor(client):
    """Returned on every listing, so a client can switch to the walk without
    composing a token out of fields it would then have to keep composing
    correctly."""
    seed(30)
    body = client.get("/signals?limit=10", headers=_h()).json()
    assert body["next_cursor"]
    assert body["offset"] == 0


def test_a_cursor_page_reports_what_remains(client):
    """`total` counts what is left, because the cursor clause is part of the
    filter — a caller resuming has no use for a count of rows it has read."""
    seed(30)
    first = client.get("/signals?limit=10", headers=_h()).json()
    assert first["total"] == 30
    second = client.get(f"/signals?limit=10&cursor={first['next_cursor']}",
                        headers=_h()).json()
    assert second["total"] == 20
    assert "offset" not in second, "a cursor page reports no offset to resume from"


# --- the refusals -----------------------------------------------------------------

def test_a_cursor_and_an_offset_together_are_refused(client):
    """Two answers to one question. Honouring one silently would serve a
    different page than the caller asked for."""
    seed(5)
    body = client.get("/signals?limit=2", headers=_h()).json()
    r = client.get(f"/signals?limit=2&offset=2&cursor={body['next_cursor']}",
                   headers=_h())
    assert r.status_code == 400
    assert "not both" in r.json()["error"]


def test_an_offset_of_zero_is_still_an_offset(client):
    """`offset=0` arrived as a choice the caller typed, not as the default, so it
    collides with a cursor exactly as any other offset does. The code reads
    presence rather than truthiness; this is the test that says so."""
    seed(10)
    body = client.get("/signals?limit=4", headers=_h()).json()
    r = client.get(f"/signals?cursor={body['next_cursor']}&offset=0", headers=_h())
    assert r.status_code == 400
    assert "not both" in r.json()["error"]


def test_a_malformed_cursor_is_refused(client):
    seed(5)
    for bad in ("nonsense", "|", "2026-08-01 12:00:00.000000|", "|sig_00001"):
        r = client.get(f"/signals?cursor={bad}", headers=_h())
        assert r.status_code == 400, bad
        assert "cursor" in r.json()["error"], bad


def test_a_cursor_on_the_nullable_order_is_refused(client):
    """`produced_at` is nullable, so a keyset walk across it needs a different
    predicate. Refused rather than guessed — a silently wrong page would not say
    so."""
    seed(5)
    body = client.get("/signals?limit=2", headers=_h()).json()
    r = client.get(f"/signals?order=produced_at&cursor={body['next_cursor']}",
                   headers=_h())
    assert r.status_code == 400
    assert "default order" in r.json()["error"]


def test_the_cursor_respects_the_filters_it_was_taken_under(client):
    """A walk narrowed by `kind` stays narrowed — the cursor is a position in the
    listing, not a position in the table."""
    seed(20)
    conn = sqlite3.connect(store.DB_PATH)
    for i in range(20, 30):
        conn.execute(
            "INSERT INTO signals (id, source, kind, actor, product, body, "
            "privacy_class, ingested_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"sig_{i:05d}", "test", "alert", "someone", "keel", "b", "internal",
             f"2026-08-01 13:00:{i:02d}.000000"))
    conn.commit()
    conn.close()
    ids, _ = walk(client, query="&kind=alert", limit=4)
    assert len(ids) == 10
    assert all(int(i.removeprefix("sig_")) >= 20 for i in ids)


def _seed_reversed_produced_at(n):
    """`n` signals whose `produced_at` order is the exact reverse of their
    `ingested_at` order, which is what makes a cursor minted under one order
    demonstrably wrong in the other."""
    conn = sqlite3.connect(store.DB_PATH)
    for i in range(n):
        conn.execute(
            "INSERT INTO signals (id, source, kind, actor, product, body, "
            "privacy_class, ingested_at, produced_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"sig_{i:05d}", "test", "note", "someone", "keel", f"body {i}",
             "internal", f"2026-08-01 12:00:00.{i:06d}",
             f"2026-08-01 11:00:00.{n - 1 - i:06d}"))
    conn.commit()
    conn.close()


def test_a_listing_in_the_nullable_order_offers_no_cursor(client):
    """**The round-1 blocking finding.** `next_cursor` was minted for every
    listing while only an EXPLICIT `order=produced_at` was refused on the way back
    in, so the two ends disagreed about which orders a cursor covers. A client
    that pages `produced_at`, takes the token, and resumes WITHOUT re-sending the
    parameter — which the API never asked it to, the token looking self-contained
    — passed the consumption check and resumed an `ingested_at` walk from a
    boundary row that has no position in it.

    Reproduced by the security reviewer against 20 rows: page 2 repeated 4 rows
    already served, never served 15 of the 20, and reported `next_cursor: null`
    as though the walk had finished."""
    _seed_reversed_produced_at(20)
    page = client.get("/signals?order=produced_at&limit=5", headers=_h()).json()
    assert page["truncated"], "the page is short of the table, so a walk would continue"
    assert page["next_cursor"] is None, (
        "a cursor minted here names a position in ingested_at order, which is not "
        "the order this page was served in")


def test_the_default_order_named_explicitly_still_offers_a_cursor(client):
    """Omitting `order` and naming the default are the same request, so the
    refusal must not catch the one that spells it out."""
    seed(10)
    body = client.get("/signals?order=ingested_at&limit=4", headers=_h()).json()
    assert body["next_cursor"] is not None
    ids, _ = walk(client, query="&order=ingested_at", limit=4)
    assert len(ids) == 10


def test_every_supported_order_agrees_at_both_ends(client):
    """The invariant the finding broke, stated so a THIRD order cannot break it
    again: if a listing in some order hands out a cursor, that same order must
    accept it back. Minting where consumption refuses is the silent-wrong-walk
    bug; refusing what was minted is merely a walk that stops early."""
    _seed_reversed_produced_at(20)
    for order in libapp.SIGNAL_ORDER_CLAUSES:
        body = client.get(f"/signals?order={order}&limit=5", headers=_h()).json()
        token = body["next_cursor"]
        if token is None:
            continue
        resumed = client.get(f"/signals?order={order}&cursor={token}", headers=_h())
        assert resumed.status_code == 200, (
            f"{order} minted a cursor it will not accept back")


def test_dropping_the_order_on_resume_cannot_reach_a_wrong_walk(client):
    """The security reviewer's repro, which is the silent path: the client never
    re-sends `order` on the follow-up, because the token looks self-contained.
    Under the old code that request passed the consumption check — `order` was
    `None`, not `produced_at` — and resumed an `ingested_at` walk from a boundary
    row with no position in it.

    The fix closes it at the mint: there is no token to carry into that request."""
    _seed_reversed_produced_at(20)
    page = client.get("/signals?order=produced_at&limit=5", headers=_h()).json()
    served = [s["id"] for s in page["signals"]]
    assert page["next_cursor"] is None, "nothing to resume with, so nothing to get wrong"
    # And the default order, which DOES hand one out, resumes without repeating.
    first = client.get("/signals?limit=5", headers=_h()).json()
    again = client.get(f"/signals?cursor={first['next_cursor']}&limit=5",
                       headers=_h()).json()
    assert not set(s["id"] for s in first["signals"]) & set(
        s["id"] for s in again["signals"]), "the walk that IS offered repeats nothing"
    assert served, "the produced_at page still serves its own rows"
