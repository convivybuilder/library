"""The Library's read surface, against the protocol contract.

Two things this file is really holding.

**The protocol version is required on every route, and a route added later cannot
forget it.** That is asserted by enumerating the app's own routes rather than by
listing the ones that exist today — a test that names endpoints passes forever
while the endpoint someone adds next month skips the check.

**Scoping is a read boundary, and `total` is part of it.** A caller must not learn
that a row exists in a product it cannot see, including by reading a count that
includes it. Every list test checks the count as well as the rows, because
filtering after the query is exactly how a `total` ends up telling the truth to
someone who should not have it.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json
import pathlib
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import projects
import protocol
import reference
import store
import vectors

AGENT = "agent-token-vivy"
WIDE = "agent-token-wide"
SYSTEM = "system-token"
NOSCOPE = "agent-token-noscope"
# THREE PRODUCTS, REGISTERED OUT OF ORDER. Every other token here carries zero or
# one, which leaves `sorted(ident.products)` asserted by nothing — `products`
# lives on a frozenset, whose iteration order is arbitrary, so an unsorted
# response would be flakily right rather than wrong (reviewer, PR #29).
MULTI = "agent-token-multi"


def _window_safe_ts(days_ago, *, microseconds=True):
    """A timestamp computed at fixture time, `days_ago` before whatever "now"
    the process's `datetime.now()` resolves to.

    Use this for any row that a test asserts to be INSIDE a rolling window
    (`librarian_metrics.DEFAULT_WINDOW_DAYS` and friends) — a value pinned to
    a calendar date instead falls out of a 30-day window exactly 30 days
    after the date it names, which is the failure
    issue_01M1HPV2VS51RFGF3X8ZJTD4EH diagnoses. A row asserted only for
    ORDERING relative to other rows may stay pinned; nothing about ordering
    expires.

    `days_ago` must stay well inside the window every caller cares about
    (small, single-digit values), since both this helper and the metric it
    feeds read the same "now" at test time — the offset does not need to
    track the window size, it only needs to never approach it.
    """
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    ts = now - _dt.timedelta(days=days_ago)
    return ts.strftime("%Y-%m-%d %H:%M:%S.%f" if microseconds
                        else "%Y-%m-%d %H:%M:%S")


def _build_client(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE issue_items (
            id TEXT PRIMARY KEY, product TEXT, title TEXT, body TEXT, refs TEXT,
            severity TEXT, state TEXT, claimed_by TEXT, claimed_by_crew TEXT, claimed_at TEXT,
            resolution TEXT, resolved_by TEXT, resolved_at TEXT, outcome_note TEXT,
            once_key TEXT, created_by TEXT, created_at TEXT, run_id TEXT, type TEXT,
            created_by_kind TEXT, needs TEXT, park_reason TEXT,
            unpark_condition TEXT, parked_at TEXT, embedded_at TEXT,
            parent_id TEXT, topics TEXT, maturity TEXT, specification TEXT,
            definition_of_done TEXT, prerequisites TEXT,
            model_choice TEXT, model_reason TEXT, model_set_by_client TEXT,
            model_acting_human TEXT, model_set_at TEXT, found_during TEXT, item_key TEXT, short_description TEXT);
        CREATE TABLE issue_item_key_counters (
            scope_kind TEXT, scope_id TEXT, next_value INTEGER,
            PRIMARY KEY (scope_kind, scope_id));
        CREATE TABLE issue_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, actor TEXT,
            from_state TEXT, to_state TEXT, note TEXT, at TEXT);
        CREATE TABLE course_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, course_id TEXT, actor TEXT,
            action TEXT, entries TEXT, waypoints TEXT, note TEXT, at TEXT);
        CREATE TABLE issue_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, actor TEXT,
            kind TEXT, body TEXT, run_id TEXT, at TEXT);
        CREATE TABLE courses (
            id TEXT PRIMARY KEY, product TEXT, title TEXT, entries TEXT,
            waypoints TEXT DEFAULT '[]',
            status TEXT, version INTEGER, created_by TEXT, created_at TEXT,
            updated_by TEXT, updated_at TEXT);
        CREATE TABLE issue_leases (
            id TEXT PRIMARY KEY, root_id TEXT, taken_for TEXT, holder TEXT, holder_crew TEXT,
            run_id TEXT, taken_at TEXT, heartbeat_at TEXT, released_at TEXT,
            release_reason TEXT, released_by TEXT, note TEXT);
        CREATE UNIQUE INDEX ix_live_root ON issue_leases (root_id)
            WHERE released_at IS NULL;
        CREATE TABLE lease_refusals (
            id TEXT PRIMARY KEY, at TEXT, requested_item_id TEXT, product TEXT,
            requester TEXT, actor TEXT, blocking_lease_id TEXT,
            blocking_root_id TEXT, blocking_holder TEXT, blocks_by TEXT, blocking_taken_for TEXT);
        CREATE TABLE signals (
            id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
            ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
            privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
            embedded_at TEXT, dedup_key TEXT, acting_human TEXT);
        CREATE INDEX ix_signals_test_result_commit ON signals
            (json_extract(refs, '$.commit')) WHERE kind = 'test-result';
        CREATE TABLE issue_epic_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, epic_id TEXT,
            created_by TEXT, created_at TEXT);
    """)
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, type, state, created_at, "
        "once_key, topics) VALUES (?,?,?,?,?,?,?,?)",
        # Stored the way the live vault stores them — a space separator and
        # microseconds — so the serialisation the protocol promises is exercised
        # rather than assumed. A date-only fixture value would pass whether or
        # not the Library reformats anything.
        #
        # `issue_v1` and `issue_c1` stay PINNED to a calendar date: nothing in
        # this file asserts an order between them, or between them and any
        # other row, so there is no window for a pinned date to fall out of.
        # `issue_c1` is the one exception — it is asserted to be INSIDE
        # `librarian_metrics`' rolling `DEFAULT_WINDOW_DAYS` window
        # (`test_librarian_metrics_counts_duplicate_filings_in_the_window`),
        # so it is computed relative to test-run time instead
        # (issue_01M1HPV2VS51RFGF3X8ZJTD4EH — a pinned date there fell out of
        # its own window exactly 30 days after the date it named).
        [("issue_v1", "vivy", "A vivy item", "bug", "open",
          "2026-08-01 12:00:00.111111", "k1", '["a"]'),
         ("issue_v2", "vivy", "A vivy epic", "epic", "open",
          "2026-08-02 12:00:00.222222", "k2", "[]"),
         ("issue_c1", "constance", "A constance item", "bug", "open",
          _window_safe_ts(2), "k3", "[]")])
    conn.executemany(
        "INSERT INTO courses (id, product, title, entries, status, version, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        # THE TWO LIVE COURSES SORT ONE WAY BY `created_at` AND THE OTHER BY
        # `updated_at`, on purpose. Ordering is the one thing about this list a
        # test can get wrong while every assertion still passes, and the shadow
        # harness caught the Library listing by `updated_at` where Orient lists
        # by `created_at`. `course_c` also carries no `updated_at` at all, which
        # is the live shape that made the old ordering arbitrary rather than
        # merely different — an unrevised Course sorted wherever SQLite puts
        # NULL.
        #
        # The vivy Course carries a cross-product entry (`issue_c1`) alongside a
        # dangling one. Both are data-integrity faults rather than normal states,
        # and both have to resolve to `missing` rather than to a disclosure or a
        # silently shorter list.
        [("course_v", "vivy", "Vivy's route",
          json.dumps(["issue_v2", "issue_gone", "issue_c1"]), "live", 3,
          "2026-08-06 09:15:00.123456", "2026-08-04 11:00:00.000000"),
         # A SECOND COURSE IN ANOTHER PRODUCT. Without it every scope assertion on
         # this route is vacuous — one row and one scope means the filter can
         # never remove anything (reviewer, PR #1).
         ("course_c", "constance", "Constance's route", json.dumps([]),
          "live", 1, "2026-08-09 08:00:00.000000", None),
         ("course_old", "vivy", "What vivy used to intend", json.dumps([]),
          "superseded", 1, "2026-08-01 10:00:00.000000",
          "2026-08-03 10:00:00.000000")])
    conn.executemany(
        "INSERT INTO signals (id, product, kind, body, privacy_class, ingested_at) "
        "VALUES (?,?,?,?,?,?)",
        [("sig_v1", "vivy", "note", "an internal note", "internal",
          "2026-08-01 07:30:00.444444"),
         ("sig_v2", "vivy", "note", "a customer said this", "customer",
          "2026-08-02 07:30:00.555555")])
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        WIDE: {"name": "wide-agent", "products": ["*"]},
        SYSTEM: {"name": "capture", "products": ["*"], "kind": "system"},
        NOSCOPE: {"name": "scopeless", "products": []},
        MULTI: {"name": "many-products",
                "products": ["vivy", "constance", "hollis"]},
    }))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    return TestClient(libapp.app)


@pytest.fixture
def client(tmp_path, monkeypatch):
    return _build_client(tmp_path, monkeypatch)


def _h(token=AGENT, version=protocol.CURRENT):
    h = {"Authorization": f"Bearer {token}"}
    if version is not None:
        h[protocol.HEADER] = str(version)
    return h


# --- the protocol version ------------------------------------------------------


def test_every_route_requires_the_protocol_header_except_health(client):
    """ENUMERATED FROM THE APP, not from a list written here. A test naming the
    endpoints that exist today keeps passing while the one added next month skips
    negotiation entirely — which is the whole failure the middleware exists to
    make impossible."""
    paths = {r.path for r in libapp.app.routes
             if getattr(r, "methods", None) and "GET" in r.methods}
    assert len(paths) > 1, "route enumeration found nothing; the assertion is vacuous"
    for path in paths:
        # Substitute ANY path parameter generically. Naming the three that exist
        # today would send a literal `{document_id}` at a route added later — the
        # middleware fires before routing, so the test would pass while checking a
        # path that does not exist (reviewer, PR #1).
        concrete = re.sub(r"\{[^}]+\}", "nope", path)
        r = client.get(concrete, headers={"Authorization": f"Bearer {AGENT}"})
        if concrete == "/health":
            assert r.status_code == 200, "health must answer without the header"
            continue
        assert r.status_code == 400, f"{concrete} did not require the version"
        assert protocol.HEADER in r.json()["error"]


def test_an_unservable_version_is_refused_and_names_what_is_served(client):
    r = client.get("/items", headers=_h(version=99))
    assert r.status_code == 400
    assert r.json()["supported"] == list(protocol.SUPPORTED)


def test_a_non_integer_version_is_refused(client):
    r = client.get("/items", headers=_h(version="one"))
    assert r.status_code == 400
    assert "integer" in r.json()["error"]


def test_the_response_names_the_version_it_served(client):
    r = client.get("/items", headers=_h())
    assert r.headers[protocol.HEADER] == str(protocol.CURRENT)


def test_health_reports_what_it_speaks_without_authenticating(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["protocol"]["supported"] == list(protocol.SUPPORTED)


# --- identity and scoping ------------------------------------------------------


def test_no_credential_is_401(client):
    r = client.get("/items", headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401


def test_an_unknown_token_is_401_rather_than_a_role(client):
    r = client.get("/items", headers=_h(token="not-a-real-token"))
    assert r.status_code == 401


def test_a_list_is_scoped_and_the_total_is_scoped_with_it(client):
    """The count is part of the boundary. A `total` computed before the scope
    filter tells an out-of-scope caller how much it cannot see."""
    r = client.get("/items", headers=_h())
    body = r.json()
    assert {i["product"] for i in body["items"]} == {"vivy"}
    assert body["total"] == 2, "total counted rows outside the caller's scope"


def test_a_wildcard_identity_sees_every_product(client):
    body = client.get("/items", headers=_h(WIDE)).json()
    assert body["total"] == 3


def test_an_identity_scoped_to_nothing_reads_nothing(client):
    """Authenticated and scoped to no product is a real state, not a bug. It must
    resolve to an empty result rather than to everything."""
    body = client.get("/items", headers=_h(NOSCOPE)).json()
    assert body["items"] == [] and body["total"] == 0


def test_an_out_of_scope_item_is_404_not_403(client):
    """Splitting "no such item" from "not yours" would make the pair an existence
    oracle: a caller could tell a real id from an invented one by the refusal."""
    real = client.get("/items/issue_c1", headers=_h())
    invented = client.get("/items/issue_nope", headers=_h())
    assert real.status_code == invented.status_code == 404
    assert real.json() == invented.json()


# --- what the reads return -----------------------------------------------------


def test_an_item_carries_the_protocol_fields_and_not_the_write_key(client):
    body = client.get("/items/issue_v1", headers=_h()).json()
    assert body["id"] == "issue_v1" and body["topics"] == ["a"]
    # `once_key` belongs to the write path — publishing it would let a reader
    # forge a collision with somebody else's pending write.
    assert "once_key" not in body
    assert "embedded_at" not in body


def test_pagination_reports_count_total_and_truncation(client):
    body = client.get("/items?limit=1", headers=_h(WIDE)).json()
    assert body["count"] == 1 and body["total"] == 3 and body["truncated"] is True
    last = client.get("/items?limit=1&offset=2", headers=_h(WIDE)).json()
    assert last["truncated"] is False


def _seed_tied_items(n=6, product="vivy"):
    """`n` items sharing one `created_at`, inserted so that ROWID order is the
    REVERSE of id order.

    That inversion is the whole design of this fixture. Tied rows come back in
    rowid order when nothing else decides, so seeding them in id order would
    produce a test that passes with or without a tiebreaker — pinning SQLite's
    incidental behaviour and calling it a guarantee. Inserting backwards means
    the two orders disagree, and only the query's own `ORDER BY ..., id` can
    produce the ascending one.
    """
    conn = sqlite3.connect(store.DB_PATH)
    ids = [f"issue_tie_{chr(ord('a') + i)}" for i in range(n)]
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, type, state, created_at, "
        "once_key, topics) VALUES (?,?,?,?,?,?,?,?)",
        [(i, product, f"tied {i}", "bug", "open", "2026-08-05 09:00:00.000000",
          f"k-{i}", "[]") for i in reversed(ids)])
    conn.commit()
    conn.close()
    return ids


TIE_TS = "2026-08-05 09:00:00.000000"


def _insert(table, cols, rows):
    conn = sqlite3.connect(store.DB_PATH)
    conn.executemany(
        f"INSERT INTO {table} ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))})", rows)
    conn.commit()
    conn.close()


def _tie_ids(prefix, n=4):
    return [f"{prefix}{chr(ord('a') + i)}" for i in range(n)]


# Each seeder inserts its rows in REVERSED id order, so ROWID order and id order
# disagree. That inversion is the whole design: tied rows come back in rowid
# order when nothing else decides, so seeding in id order would make every test
# below pass with or without a tiebreaker — pinning SQLite's incidental
# behaviour and calling it a guarantee.


def _seed_items(ids):
    _insert("issue_items",
            ("id", "product", "title", "type", "state", "created_at",
             "once_key", "topics"),
            [(i, "vivy", f"tied {i}", "bug", "open", TIE_TS, f"k-{i}", "[]")
             for i in reversed(ids)])


def _seed_courses(ids):
    _insert("courses",
            ("id", "product", "title", "entries", "status", "version",
             "created_at"),
            [(i, "vivy", f"tied {i}", "[]", "live", 1, TIE_TS)
             for i in reversed(ids)])


def _seed_signals(ids):
    _insert("signals",
            ("id", "product", "kind", "body", "privacy_class", "ingested_at"),
            [(i, "vivy", "note", f"tied {i}", "internal", TIE_TS)
             for i in reversed(ids)])


def _seed_signals_produced_at(ids):
    """BOTH `produced_at` AND `ingested_at` tie, because the clause is
    `produced_at DESC NULLS LAST, ingested_at DESC, id` — leaving `ingested_at`
    distinct would let the second term decide and the test would never reach the
    third. The most syntactically complex clause in the set, and worth its own
    case for that reason (reviewer, PR #31)."""
    _insert("signals",
            ("id", "product", "kind", "body", "privacy_class", "ingested_at",
             "produced_at"),
            [(i, "vivy", "note", f"tied {i}", "internal", TIE_TS, TIE_TS)
             for i in reversed(ids)])


def _seed_tied_leases(ids):
    """**The join-qualified shape, and the one the last round wrongly believed
    covered** (reviewer, PR #31). `list_leases` joins `issue_leases` to
    `issue_items` and scopes through the item's product, so its tiebreaker has to
    be written `issue_leases.id` rather than bare `id` to parse at all — which
    makes it the clause most likely to break on a copy-paste edit, and it had no
    test.

    Each lease needs its own root item: a partial unique index forbids two live
    leases on one root, and the join drops a lease whose root is missing.

    **The root ids run OPPOSITE to the lease ids, and reversed insertion alone
    was not enough.** The first version of this seeder gave `lease_tie_a` root
    `issue_root_a` and passed against a query with no tiebreaker at all — the
    partial unique index on `root_id` drives the join, so rows arrive in root
    order, which was also lease-id order. Inverting the pairing makes the
    index-driven order the reverse of the expected one, so only the query's own
    `issue_leases.id` can produce it. Exactly the failure this whole file is
    about, met while writing the test for it.
    """
    pairs = list(zip(ids, reversed(ids)))          # lease_tie_a → issue_root_d
    _insert("issue_items",
            ("id", "product", "title", "type", "state", "created_at",
             "once_key", "topics"),
            [(f"issue_root_{r[-1]}", "vivy", f"root of {i}", "epic", "open",
              TIE_TS, f"k-root-{r[-1]}", "[]") for i, r in pairs])
    _insert("issue_leases",
            ("id", "root_id", "taken_for", "holder", "run_id", "taken_at",
             "heartbeat_at"),
            [(i, f"issue_root_{r[-1]}", f"issue_root_{r[-1]}", "holder-tie",
              "run-tie", TIE_TS, TIE_TS) for i, r in reversed(pairs)])


# One entry per ordered query: a label, the route, the response key, the id
# prefix, and the seeder. Deliberately NOT all nine clauses the PR touched — the
# five here are the distinct shapes. `decisions` is textually the same
# unqualified `created_at DESC, id` as `items`; `course entries` is that shape on
# the same table; and signal facets tiebreak on a column already unique per row
# under its GROUP BY. Extending to those would be churn (reviewer, PR #31).
TIED_QUERIES = [
    ("items", "/items?limit=200", "items", "issue_tie_", _seed_items),
    ("courses", "/courses?limit=200", "courses", "course_tie_", _seed_courses),
    ("signals", "/signals?limit=200", "signals", "sig_tie_", _seed_signals),
    ("signals-produced-at", "/signals?order=produced_at&limit=200", "signals",
     "sigp_tie_", _seed_signals_produced_at),
    ("leases", "/leases?limit=200", "leases", "lease_tie_", _seed_tied_leases),
]


@pytest.mark.parametrize("label,path,key,prefix,seed",
                         TIED_QUERIES, ids=[q[0] for q in TIED_QUERIES])
def test_tied_rows_have_an_order_the_query_decides_rather_than_sqlite(
        client, label, path, key, prefix, seed):
    """SQLite leaves the order among tied rows unspecified, so a sort on one
    column makes the determinism the contract promises a property of the
    implementation rather than of the query.

    **One case per distinct SHAPE of clause, not one per table.** Round 1 tested
    only `items`, so planting the removal on the other eight changed nothing any
    test could see. Round 2 added courses and signals and claimed to cover every
    shape, which was wrong in the way that mattered: neither of them joins, and
    the join-qualified clause is the one that needs a qualified column name to
    parse. It is here now, with the three-column `NULLS LAST` clause beside it.
    """
    ids = _tie_ids(prefix)
    seed(ids)
    body = client.get(path, headers=_h(WIDE)).json()
    got = [r["id"] for r in body[key] if r["id"].startswith(prefix)]
    assert got == ids


def test_the_reference_search_breaks_a_bm25_tie_on_the_document_id(client, kb):
    """The other join-qualified clause: `ORDER BY bm25(...), d.id` over
    `docs_fts JOIN docs d`. Four documents carry identical searchable text, so
    BM25 scores them the same and only `d.id` can order them.

    Seeded through the `kb` fixture's own index rather than the control DB, since
    the Reference Room is a separate file.
    """
    ids = _tie_ids("platform/reference/tie-")
    conn = sqlite3.connect(reference.INDEX_DB)
    conn.executemany(
        "INSERT INTO docs (id, source_ref, kind, project, title, status, "
        "topics, audience, body, last_reviewed, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(i, f"{i}.md", "reference", "platform", "tiebreaker fixture",
          "current", '["tiebreaker"]', '["team","coder"]',
          "identical body text for the tiebreaker fixture", "2026-08-01",
          "2026-08-02") for i in reversed(ids)])
    # INDEX ONLY THE NEW ROWS. `docs_fts` is an external-content table over
    # `docs`, so a `DELETE FROM docs_fts` and rebuild leaves the index
    # inconsistent with its content table — SQLite reports it as `database disk
    # image is malformed` on the next read.
    conn.execute("INSERT INTO docs_fts(rowid, id, title, body, topics) "
                 "SELECT rowid, id, title, body, topics FROM docs "
                 "WHERE id LIKE 'platform/reference/tie-%'")
    conn.commit()
    conn.close()

    body = client.get("/reference/search?q=tiebreaker&limit=50",
                      headers=_h()).json()
    got = [d["id"] for d in body["documents"] if d["id"].startswith(
        "platform/reference/tie-")]
    assert got == ids


def test_a_tie_pages_without_dropping_or_repeating_a_row(client):
    """The failure the tiebreaker actually prevents, and the contract clause it
    keeps: the same query against unchanged corpus state returns the same result
    set. Under LIMIT/OFFSET an unspecified order lets a tied row land on two
    pages or on neither, because each page is a fresh query free to order the tie
    differently.

    A separate "call it twice, get the same list" test used to sit here and was
    dropped (reviewer, PR #31). It could not fail: SQLite is self-consistent
    within a process, so two identical queries agree with each other whether or
    not the SQL specifies a total order, and the assertion above already implies
    it. Paging is where the promise is actually testable, because each page is a
    separate query.
    """
    ids = _seed_tied_items()
    paged = []
    for offset in range(0, 12, 2):
        page = client.get(f"/items?limit=2&offset={offset}",
                          headers=_h()).json()["items"]
        paged.extend(r["id"] for r in page)
    tied = [i for i in paged if i.startswith("issue_tie_")]
    assert tied == ids, "paging did not reproduce the unpaged order"
    assert len(tied) == len(set(tied)), "a tied row appeared on two pages"


def test_an_over_cap_limit_is_clamped_rather_than_refused(client):
    """A caller cannot guess the ceiling, so refusing is a worse answer than
    returning fewer rows and saying there are more."""
    r = client.get(f"/items?limit={libapp.MAX_PAGE + 500}", headers=_h(WIDE))
    assert r.status_code == 200


def test_a_malformed_limit_is_refused_rather_than_defaulted(client):
    """Silently reading `"ten"` as 50 hides a client bug indefinitely."""
    r = client.get("/items?limit=ten", headers=_h(WIDE))
    assert r.status_code == 400


def test_items_list_refuses_an_unrecognised_parameter(client):
    """A dropped filter hands back MORE than was asked for, which is the
    direction of failure issue_01M1MTCTH3PW0DWDEXAZA5KFET exists to close."""
    r = client.get("/items?priority=high", headers=_h(WIDE))
    assert r.status_code == 400
    assert r.json()["unknown"] == ["priority"]
    assert r.json()["supported"] == sorted(
        ["found_during", "home", "limit", "needs", "offset", "parent_id",
         "product", "sort", "state", "topics", "type"])


def test_items_list_ordinary_request_still_succeeds(client):
    r = client.get("/items?product=vivy&state=open&limit=5&offset=0"
                   "&sort=created_at", headers=_h(WIDE))
    assert r.status_code == 200


def test_item_get_refuses_any_parameter(client):
    r = client.get("/items/issue_v1?priority=high", headers=_h())
    assert r.status_code == 400
    assert r.json()["unknown"] == ["priority"]
    assert r.json()["supported"] == []


def test_a_course_resolves_its_entries_and_reports_a_missing_one(client):
    """A silently shorter list reads as a plan with fewer steps. `missing` says
    the plan points at something that no longer exists."""
    body = client.get("/courses/course_v", headers=_h()).json()
    assert [i["id"] for i in body["items"]] == ["issue_v2", "issue_gone", "issue_c1"]
    assert body["items"][1]["missing"] is True
    assert "missing" not in body["items"][0]


def test_a_cross_product_course_entry_is_not_resolved_into_a_disclosure(client):
    """Nothing enforces that a Course entry names an item in the Course's own
    product, so a cross-product entry is a data-integrity fault the read surface
    must not turn into a leak. It reads as `missing` — the caller cannot see it,
    and saying so beats both dropping it and disclosing it."""
    body = client.get("/courses/course_v", headers=_h()).json()
    entry = body["items"][2]
    assert entry["id"] == "issue_c1" and entry["missing"] is True
    assert "title" not in entry, "a cross-product entry disclosed its body"
    # And a wildcard caller, which MAY see it, gets the real item.
    wide = client.get("/courses/course_v", headers=_h(WIDE)).json()
    assert wide["items"][2]["title"] == "A constance item"


def test_a_course_list_is_scoped_and_the_total_is_scoped_with_it(client):
    """The same invariant items and signals carry. Without a second Course in
    another product this assertion could not fail (reviewer, PR #1)."""
    body = client.get("/courses", headers=_h()).json()
    assert [c["id"] for c in body["courses"]] == ["course_v"]
    assert body["total"] == 1, "the total counted a Course outside the caller's scope"
    wide = client.get("/courses", headers=_h(WIDE)).json()
    assert wide["total"] == 2, "a wildcard caller should see both live Courses"


def test_a_course_list_resolves_product_through_the_registry(client, tmp_path, monkeypatch):
    """A literal match on `product` is why an alias or a non-canonical casing
    silently returned nothing where Orient resolves the query param first
    (reviewer, PR #23)."""
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"vivy": {"aliases": ["viv"], "repos": []}}))
    with monkeypatch.context() as m:
        m.setattr(projects, "REGISTRY_PATH", registry)
        projects.reload()
        alias = client.get("/courses?product=viv", headers=_h(WIDE)).json()
        cased = client.get("/courses?product=VIVY", headers=_h(WIDE)).json()
    projects.reload()
    assert [c["id"] for c in alias["courses"]] == ["course_v"]
    assert [c["id"] for c in cased["courses"]] == ["course_v"]


def test_a_course_list_paginates_like_every_other_list(client):
    """A client reads a MISSING `total` as "this page may be incomplete", which is
    the right signal from an older server and a wrong one from the first one."""
    body = client.get("/courses?limit=1", headers=_h(WIDE)).json()
    assert body["count"] == 1 and body["total"] == 2
    assert body["truncated"] is True and body["offset"] == 0
    page2 = client.get("/courses?limit=1&offset=1", headers=_h(WIDE)).json()
    assert page2["truncated"] is False
    assert page2["courses"][0]["id"] != body["courses"][0]["id"]


def test_course_list_refuses_an_unrecognised_parameter(client):
    r = client.get("/courses?title=anything", headers=_h(WIDE))
    assert r.status_code == 400
    assert r.json()["unknown"] == ["title"]
    assert r.json()["supported"] == ["limit", "offset", "product", "status"]


def test_course_get_refuses_any_parameter(client):
    r = client.get("/courses/course_v?title=anything", headers=_h())
    assert r.status_code == 400
    assert r.json()["unknown"] == ["title"]
    assert r.json()["supported"] == []


def test_a_course_list_is_ordered_by_creation_the_way_orient_orders_it(client):
    """**The shadow harness found this, and no test here would have.**

    The Library listed by `updated_at DESC` where Orient lists by
    `created_at DESC`, so the two services returned the same Courses in opposite
    orders for every wildcard identity. Every existing assertion on this route
    compares sets or single-element lists, which is why a wrong order survived
    them all.

    The fixture is built so the two orderings disagree: `course_c` was created
    last and has never been revised, `course_v` was created earlier and revised
    since. Ordering by `updated_at` puts `course_v` first and fails here.
    """
    body = client.get("/courses", headers=_h(WIDE)).json()
    assert [c["id"] for c in body["courses"]] == ["course_c", "course_v"]


def test_a_course_that_was_never_revised_still_sorts_by_when_it_was_created(client):
    """`updated_at` is nullable, so the old ordering did not merely differ from
    Orient's — it had no defined answer for a Course nobody has edited, and put it
    wherever SQLite happens to sort NULL. `course_c` is that row, and it is the
    live shape: the `convivy-deck` Course carries no `updated_at` today."""
    body = client.get("/courses", headers=_h(WIDE)).json()
    unrevised = [c for c in body["courses"] if c["id"] == "course_c"][0]
    assert unrevised["updated_at"] is None
    assert body["courses"][0]["id"] == "course_c", (
        "a Course with no updated_at sorted somewhere other than its created_at")


# --- timestamp serialisation ---------------------------------------------------


TIMESTAMP_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@pytest.mark.parametrize("path,key,fields", [
    ("/items", "items", ("created_at",)),
    ("/courses", "courses", ("created_at",)),
    ("/signals", "signals", ("ingested_at",)),
])
def test_every_timestamp_is_rfc3339_the_way_orient_serialises_it(
        client, path, key, fields):
    """**Also found by the shadow harness, and the reason it does not normalise.**

    SQLite hands back the stored text, so these routes were answering
    `2026-08-01 12:00:00.111111` where Orient answers `2026-08-01T12:00:00Z`.
    Same instant, and an RFC-3339 parser rejects the first. Every client of these
    routes today is a client of Orient's, so this is what makes the Library a
    drop-in rather than a migration each caller has to notice.

    A harness that coerced both sides into datetimes before comparing would have
    called this agreement, which is why the comparison classes formatting
    separately instead of absorbing it.
    """
    body = client.get(path, headers=_h(WIDE)).json()
    assert body[key], "the fixture served no rows, so this asserts nothing"
    for row in body[key]:
        for f in fields:
            assert TIMESTAMP_Z.match(row[f]), (
                f"{path} returned {f}={row[f]!r}, which is not RFC-3339")


def test_a_null_timestamp_stays_null_rather_than_becoming_an_epoch(client):
    """The serialiser is reached with NULL on any unresolved row, and a formatter
    that turned that into a date would invent a fact — an unclaimed item would
    report having been claimed at midnight in 1970."""
    body = client.get("/items", headers=_h(WIDE)).json()
    assert all(r["claimed_at"] is None for r in body["items"])
    assert all(r["resolved_at"] is None for r in body["items"])


def test_a_timestamp_it_cannot_parse_is_passed_through_untouched():
    """Returning the raw value beats coercing to None. A column holding something
    this cannot read is a fact about the data worth surfacing; swallowing it would
    turn a visible oddity into a field that looks absent."""
    assert store._iso("not a timestamp at all") == "not a timestamp at all"
    assert store._iso("") == ""
    assert store._iso(None) is None


def test_a_timestamp_that_already_carries_a_zone_is_converted_not_relabelled():
    """The vault stores naive UTC, but the serialiser is a general one and an
    aware value must not simply have its designator swapped — that would move the
    instant by the offset while claiming it had not changed."""
    assert store._iso("2026-08-01T12:00:00+02:00") == "2026-08-01T10:00:00Z"
    assert store._iso("2026-08-01 12:00:00.123456") == "2026-08-01T12:00:00Z"


def test_courses_default_to_live(client):
    body = client.get("/courses", headers=_h()).json()
    assert [c["id"] for c in body["courses"]] == ["course_v"]
    every = client.get("/courses?status=all", headers=_h()).json()
    assert {c["id"] for c in every["courses"]} == {"course_v", "course_old"}


# --- leases ----------------------------------------------------------------


def _conn():
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_leases():
    """A live lease per product, plus a released one and a refusal — enough to
    exercise scoping, `state`, `holder` and the checkout formatter reused from
    `lease.py`. `issue_v2` (a vivy epic) and `issue_c1` (a constance item) are
    both parentless-or-epic, so each leases itself and stands in as a root."""
    conn = _conn()
    conn.executemany(
        "INSERT INTO issue_leases (id, root_id, taken_for, holder, run_id, "
        "taken_at, heartbeat_at, released_at, release_reason, released_by, "
        "note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [("lease_v1", "issue_v2", "issue_v2", "holder-vivy", "run-1",
          "2026-08-10 09:00:00", "2026-08-10 09:00:00", None, None, None,
          None),
         ("lease_c1", "issue_c1", "issue_c1", "holder-constance", "run-2",
          "2026-08-10 08:00:00", "2026-08-10 08:00:00", None, None, None,
          None),
         ("lease_v0", "issue_v1", "issue_v1", "holder-old", "run-0",
          "2026-08-01 09:00:00", "2026-08-01 09:00:00",
          "2026-08-01 10:00:00", "released", "holder-old", None)])
    # `ref_v1.at` is the second instance of the fuse
    # issue_01M1HPV2VS51RFGF3X8ZJTD4EH names: it feeds
    # `librarian_metrics.refusals`' rolling window
    # (`test_librarian_metrics_reads_lease_refusals_scoped_by_product`), so a
    # pinned date would have fallen out of the default 30-day window on
    # 2026-09-10, thirty days after the date this row used to name. Every
    # other timestamp in this fixture (the leases above) is asserted only for
    # scoping, state and format, never for window membership, so those stay
    # pinned.
    conn.execute(
        "INSERT INTO lease_refusals (id, at, requested_item_id, product, "
        "requester, actor, blocking_lease_id, blocking_root_id, "
        "blocking_holder, blocks_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("ref_v1", _window_safe_ts(1, microseconds=False), "issue_v1", "vivy",
         "someone", "someone", "lease_v1", "issue_v2", "holder-vivy",
         "ancestor"))
    conn.commit()
    conn.close()


def test_leases_are_scoped_through_the_root_items_product(client):
    """The scoping the checkout formatter already builds — a lease join, not a
    denormalized column on the lease itself."""
    _seed_leases()
    body = client.get("/leases", headers=_h()).json()
    assert [ln["root_id"] for ln in body["leases"]] == ["issue_v2"]
    assert body["total"] == 1, "total counted a lease outside the caller's scope"
    wide = client.get("/leases", headers=_h(WIDE)).json()
    assert {ln["root_id"] for ln in wide["leases"]} == {"issue_v2", "issue_c1"}
    assert wide["total"] == 2


def test_leases_default_to_live_and_state_all_includes_released(client):
    _seed_leases()
    live = client.get("/leases", headers=_h(WIDE)).json()
    assert {ln["root_id"] for ln in live["leases"]} == {"issue_v2", "issue_c1"}
    every = client.get("/leases?state=all", headers=_h(WIDE)).json()
    assert {ln["root_id"] for ln in every["leases"]} == {
        "issue_v2", "issue_c1", "issue_v1"}


def test_leases_state_param_is_validated(client):
    r = client.get("/leases?state=bogus", headers=_h())
    assert r.status_code == 400


def test_leases_holder_narrows_to_one_session(client):
    _seed_leases()
    body = client.get("/leases?holder=holder-vivy", headers=_h(WIDE)).json()
    assert [ln["holder"] for ln in body["leases"]] == ["holder-vivy"]


def test_a_dead_lease_is_listed_and_marked_dead(client):
    """Filtering dead leases out would hide exactly the rows an operator is
    looking for when something is stuck."""
    _seed_leases()
    conn = _conn()
    conn.execute(
        "UPDATE issue_leases SET taken_at = ?, heartbeat_at = ? "
        "WHERE id = 'lease_v1'", ("2020-01-01 00:00:00", "2020-01-01 00:00:00"))
    conn.commit()
    conn.close()
    body = client.get("/leases", headers=_h()).json()
    assert body["leases"][0]["dead"] is True


def test_leases_paginate_like_every_other_list(client):
    _seed_leases()
    body = client.get("/leases?limit=1", headers=_h(WIDE)).json()
    assert body["count"] == 1 and body["total"] == 2 and body["truncated"] is True


def test_leases_carry_the_checkout_response_shape_and_the_policy(client):
    """Same fields the checkout response already builds, reusing `lease.py`'s
    formatter — and no `fences`, matching Orient's listing, which does not pay
    for a subtree walk per row."""
    _seed_leases()
    body = client.get("/leases", headers=_h(WIDE)).json()
    assert set(body["policy"]) == {"grace_seconds", "max_lease_seconds"}
    row = [ln for ln in body["leases"] if ln["root_id"] == "issue_v2"][0]
    for field in ("id", "root_id", "taken_for", "holder", "taken_at",
                  "heartbeat_at", "expires_at", "dead", "released_at",
                  "release_reason", "released_by", "note"):
        assert field in row, field
    assert "fences" not in row


def test_lease_timestamps_are_rfc3339(client):
    _seed_leases()
    body = client.get("/leases", headers=_h(WIDE)).json()
    assert body["leases"], "the fixture served no rows, so this asserts nothing"
    for row in body["leases"]:
        assert TIMESTAMP_Z.match(row["taken_at"])
        assert TIMESTAMP_Z.match(row["heartbeat_at"])


def test_leases_requires_its_own_capability(client, tmp_path, monkeypatch):
    toks = tmp_path / "narrow.json"
    toks.write_text(json.dumps({"narrow": {"name": "n", "products": ["vivy"]}}))
    monkeypatch.setattr(identity, "TOKENS", toks)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", tmp_path / "no-clients.json")
    monkeypatch.setattr(identity, "ROLE_CAPS", {"agent": frozenset({"course.read"})})
    r = client.get("/leases", headers=_h("narrow"))
    assert r.status_code == 403 and "item.read" in r.json()["error"]


def test_leases_refuses_an_unrecognised_parameter(client):
    r = client.get("/leases?fences=true", headers=_h(WIDE))
    assert r.status_code == 400
    assert r.json()["unknown"] == ["fences"]
    assert r.json()["supported"] == ["holder", "limit", "offset", "state"]


def test_leases_ordinary_request_still_succeeds(client):
    _seed_leases()
    r = client.get("/leases?state=all&holder=holder-vivy&limit=5&offset=0",
                   headers=_h(WIDE))
    assert r.status_code == 200


# --- the librarian's metrics ----------------------------------------------------


def test_librarian_metrics_requires_a_product_for_a_scoped_identity(client):
    r = client.get("/metrics/librarian", headers=_h())
    assert r.status_code == 400 and "product" in r.json()["error"]


def test_librarian_metrics_refuses_a_product_outside_scope(client):
    r = client.get("/metrics/librarian?product=constance", headers=_h())
    assert r.status_code == 404


def test_librarian_metrics_a_wildcard_identity_needs_no_product(client):
    r = client.get("/metrics/librarian", headers=_h(WIDE))
    assert r.status_code == 200


def test_librarian_metrics_reports_all_four_measurements(client):
    """Two real, two `available: false` with the evidence — a metric that
    silently reads 0 because nothing feeds it is the failure ADR-0019 exists
    to catch."""
    body = client.get("/metrics/librarian?product=vivy", headers=_h()).json()
    assert body["duplicate_filing_rate"]["available"] is True
    assert body["lease_refusals"]["available"] is True
    assert body["bundle_miss_rate"]["available"] is False
    assert body["bundle_waste"]["available"] is False
    assert body["adr"] == (
        "platform/decisions/0019-the-librarian-is-deterministic-until-proven-otherwise")


def test_librarian_metrics_window_days_is_validated(client):
    r = client.get("/metrics/librarian?product=vivy&window_days=0", headers=_h())
    assert r.status_code == 400
    r2 = client.get("/metrics/librarian?product=vivy&window_days=nope",
                    headers=_h())
    assert r2.status_code == 400


def test_librarian_metrics_counts_duplicate_filings_in_the_window(client):
    """Cohorted by filing date: `issue_c1` is the only constance filing in the
    fixture, and marking it a duplicate should move both the numerator and the
    still-unresolved count."""
    conn = _conn()
    conn.execute("UPDATE issue_items SET resolution = 'duplicate', "
                 "state = 'duplicate' WHERE id = 'issue_c1'")
    conn.commit()
    conn.close()
    body = client.get("/metrics/librarian?product=constance",
                      headers=_h(WIDE)).json()
    rate = body["duplicate_filing_rate"]
    assert rate["filed"] == 1 and rate["resolved_duplicate"] == 1
    assert rate["rate"] == 1.0
    assert rate["still_unresolved"] == 0


def test_librarian_metrics_reads_lease_refusals_scoped_by_product(client):
    _seed_leases()
    body = client.get("/metrics/librarian?product=vivy", headers=_h()).json()
    refusals = body["lease_refusals"]
    assert refusals["refusals"] == 1
    assert refusals["by_direction"] == {"ancestor": 1}
    other = client.get("/metrics/librarian?product=constance",
                       headers=_h(WIDE)).json()
    assert other["lease_refusals"]["refusals"] == 0, (
        "a refusal filed under vivy leaked into another product's count")


def test_librarian_metrics_refuses_an_unrecognised_parameter(client):
    r = client.get("/metrics/librarian?product=vivy&since=2026-01-01",
                   headers=_h())
    assert r.status_code == 400
    assert r.json()["unknown"] == ["since"]
    assert r.json()["supported"] == ["product", "window_days"]


def test_librarian_metrics_window_fixtures_survive_a_year_of_clock_drift(
        tmp_path, monkeypatch):
    """DEFINITION OF DONE for issue_01M1HPV2VS51RFGF3X8ZJTD4EH: the two tests
    above must keep passing no matter when they run, not merely today.

    Moving a pinned date to today would pass right now and re-arm the exact
    same fuse thirty days out — indistinguishable from a real fix until
    someone runs it again later. Freezing the process clock a year forward
    and rebuilding the fixture under it is what tells the two apart: a
    genuinely relative fixture is computed from whatever `now` the frozen
    clock reports, so it lands the same `days_ago` before that `now` as it
    would have before today's, and the metric's own window (computed from the
    same frozen `now`) still contains it.

    The clock has to move BEFORE the fixture is built, not just for the
    metrics call — `_window_safe_ts` reads `now` at fixture time, so patching
    only around the request would leave the fixture computed from the real
    date and prove nothing.
    """
    future = (_dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
              + _dt.timedelta(days=365))

    class _AYearFromNow(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return future.replace(tzinfo=tz) if tz else future

    monkeypatch.setattr(_dt, "datetime", _AYearFromNow)
    client = _build_client(tmp_path, monkeypatch)
    _seed_leases()
    conn = _conn()
    conn.execute("UPDATE issue_items SET resolution = 'duplicate', "
                 "state = 'duplicate' WHERE id = 'issue_c1'")
    conn.commit()
    conn.close()

    dup = client.get("/metrics/librarian?product=constance",
                     headers=_h(WIDE)).json()["duplicate_filing_rate"]
    assert dup["filed"] == 1 and dup["resolved_duplicate"] == 1

    ref = client.get("/metrics/librarian?product=vivy", headers=_h()).json()
    assert ref["lease_refusals"]["refusals"] == 1


def test_librarian_metrics_requires_its_own_capability(client, tmp_path, monkeypatch):
    toks = tmp_path / "narrow.json"
    toks.write_text(json.dumps({"narrow": {"name": "n", "products": ["vivy"]}}))
    monkeypatch.setattr(identity, "TOKENS", toks)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", tmp_path / "no-clients.json")
    monkeypatch.setattr(identity, "ROLE_CAPS", {"agent": frozenset({"course.read"})})
    r = client.get("/metrics/librarian?product=vivy", headers=_h("narrow"))
    assert r.status_code == 403 and "item.read" in r.json()["error"]


# --- customer content ----------------------------------------------------------


def test_customer_signals_are_absent_from_a_plain_agents_list_and_its_total(client):
    """A coding agent has no business reading customer content, and must not learn
    such a row exists — including through a count."""
    body = client.get("/signals", headers=_h()).json()
    assert [s["id"] for s in body["signals"]] == ["sig_v1"]
    assert body["total"] == 1, "the total disclosed a customer row"


def test_a_system_identity_may_read_customer_signals(client):
    body = client.get("/signals", headers=_h(SYSTEM)).json()
    assert {s["id"] for s in body["signals"]} == {"sig_v1", "sig_v2"}


def test_a_customer_signal_reads_404_to_an_agent_not_403(client):
    """403 would confirm the row exists to an identity that may not know it does."""
    r = client.get("/signals/sig_v2", headers=_h())
    assert r.status_code == 404


def test_the_privacy_class_travels_with_the_body(client):
    """So a client can refuse to persist or forward a row it should not, rather
    than having to infer the class from the content."""
    body = client.get("/signals/sig_v1", headers=_h()).json()
    assert body["privacy_class"] == "internal"


def test_signal_get_refuses_any_parameter(client):
    r = client.get("/signals/sig_v1?reveal=true", headers=_h())
    assert r.status_code == 400
    assert r.json()["unknown"] == ["reveal"]
    assert r.json()["supported"] == []


# --- signal filters: repeatable kind/source, and the order whitelist -----------


def _seed_signal_filters():
    """Two more vivy signals distinguishing `source` and `kind` from the base
    fixture's `sig_v1`/`sig_v2`, and disagreeing with each other on
    `produced_at` vs `ingested_at` order — enough to exercise repeatable
    filters and the `order` whitelist without disturbing the two-row fixture
    every other signal test relies on."""
    conn = _conn()
    conn.executemany(
        "INSERT INTO signals (id, product, source, kind, body, privacy_class, "
        "produced_at, ingested_at, embedded_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [("sig_ci", "vivy", "ci", "build", "a ci row", "internal",
          "2026-08-03 00:00:00.000000", "2026-08-01 08:00:00.000000",
          "2026-08-01 08:05:00.000000"),
         ("sig_slack", "vivy", "slack", "chat", "a slack row", "internal",
          None, "2026-08-01 09:00:00.000000", None)])
    conn.commit()
    conn.close()


def test_signals_kind_filter_is_repeatable(client):
    _seed_signal_filters()
    body = client.get("/signals?kind=build&kind=chat", headers=_h()).json()
    assert {s["id"] for s in body["signals"]} == {"sig_ci", "sig_slack"}


def test_a_single_kind_still_filters(client):
    """`decisions`/`issues` callers already pass one `kind=`, and the repeatable
    form must not change what that continues to mean."""
    body = client.get("/signals?kind=note", headers=_h()).json()
    assert [s["id"] for s in body["signals"]] == ["sig_v1"]


def test_signals_source_filter_is_repeatable(client):
    _seed_signal_filters()
    body = client.get("/signals?source=ci&source=slack", headers=_h()).json()
    assert {s["id"] for s in body["signals"]} == {"sig_ci", "sig_slack"}


def _seed_signal_streams():
    """Three vivy signals on two distinct `stream` values, plus one
    customer-class row sharing a stream with a visible one — enough to
    exercise a repeatable `stream` filter and the customer wall holding
    through it, without disturbing the base fixture's `sig_v1`/`sig_v2`."""
    conn = _conn()
    conn.executemany(
        "INSERT INTO signals (id, product, source, kind, stream, body, "
        "privacy_class, ingested_at) VALUES (?,?,?,?,?,?,?,?)",
        [("sig_audit1", "vivy", "co", "note", "kb-audit", "an audit row",
          "internal", "2026-08-01 08:00:00.000000"),
         ("sig_release", "vivy", "co", "note", "release-notes",
          "a release row", "internal", "2026-08-01 09:00:00.000000"),
         ("sig_audit_cust", "vivy", "co", "note", "kb-audit",
          "a customer audit row", "customer", "2026-08-01 10:00:00.000000")])
    conn.commit()
    conn.close()


def test_signals_stream_filter_returns_only_that_stream(client):
    _seed_signal_streams()
    body = client.get("/signals?stream=kb-audit", headers=_h()).json()
    assert {s["id"] for s in body["signals"]} == {"sig_audit1"}


def test_signals_stream_filter_is_repeatable(client):
    """Two `stream=` values return their union, the same shape `kind` and
    `source` take."""
    _seed_signal_streams()
    body = client.get("/signals?stream=kb-audit&stream=release-notes",
                      headers=_h()).json()
    assert {s["id"] for s in body["signals"]} == {"sig_audit1", "sig_release"}


def test_signal_facets_applies_the_stream_filter(client):
    _seed_signal_streams()
    body = client.get("/signals/facets?stream=kb-audit", headers=_h()).json()
    assert body["total"] == 1
    assert body["by_source"] == [["co", 1]]


def test_a_customer_walled_stream_row_is_neither_listed_nor_counted(client):
    """The customer wall runs before `_signal_filters`, so an identity
    without `signal.customer` neither lists `sig_audit_cust` through
    `?stream=kb-audit` nor has it counted in the facet total — the same
    property `test_customer_signals_are_absent_from_a_plain_agents_list_and_its_total`
    holds for the unfiltered list."""
    _seed_signal_streams()
    plain = client.get("/signals?stream=kb-audit", headers=_h()).json()
    assert [s["id"] for s in plain["signals"]] == ["sig_audit1"]
    assert plain["total"] == 1, "the total disclosed a customer row"
    plain_facets = client.get("/signals/facets?stream=kb-audit",
                              headers=_h()).json()
    assert plain_facets["total"] == 1, "the facet total disclosed a customer row"
    with_cap = client.get("/signals?stream=kb-audit", headers=_h(SYSTEM)).json()
    assert {s["id"] for s in with_cap["signals"]} == {
        "sig_audit1", "sig_audit_cust"}


def _seed_changelog():
    """Changelog rows either side of 2026-10-11 13:00 UTC, in both stored
    `produced_at` shapes, plus one with none, for the `since` filter."""
    conn = _conn()
    conn.executemany(
        "INSERT INTO signals (id, product, source, kind, body, privacy_class, "
        "produced_at, ingested_at) VALUES (?,?,?,?,?,?,?,?)",
        [("sig_old", "vivy", "github", "changelog", "an old merge", "internal",
          "2026-10-11 12:59:59", "2026-10-11 13:00:05.000000"),
         ("sig_edge", "vivy", "github", "changelog", "a merge on the bound",
          "internal", "2026-10-11 13:00:00", "2026-10-11 13:00:06.000000"),
         ("sig_new", "vivy", "github", "changelog", "a new merge", "internal",
          "2026-10-11 13:00:00.250000", "2026-10-11 13:00:07.000000"),
         ("sig_entry", "vivy", "co", "changelog", "an entry", "internal",
          "2026-10-12 09:00:00", "2026-10-12 09:00:01.000000"),
         ("sig_undated", "vivy", "github", "changelog", "no produced_at",
          "internal", None, "2026-10-12 10:00:00.000000")])
    conn.commit()
    conn.close()


def test_signals_since_is_an_inclusive_lower_bound_on_produced_at(client):
    """`bin/changelog-drain` sends `since` on every run; before this route read
    it, each pass got the whole changelog history (issue_01M1MTCTH3PW0DWDEXAZA5KFET)."""
    _seed_changelog()
    body = client.get("/signals?kind=changelog&source=github"
                      "&since=2026-10-11T13:00:00Z", headers=_h()).json()
    assert {s["id"] for s in body["signals"]} == {"sig_edge", "sig_new"}
    assert body["total"] == 2


def test_signals_since_converts_its_zone_to_utc(client):
    _seed_changelog()
    body = client.get("/signals?kind=changelog&source=github"
                      "&since=2026-10-11T06:00:00.100000-07:00",
                      headers=_h()).json()
    assert {s["id"] for s in body["signals"]} == {"sig_new"}


def test_signals_answers_the_changelog_drains_exact_request(client):
    """The drain's two reads, parameter for parameter (agent-platform
    bin/changelog-drain `_pending`)."""
    _seed_changelog()
    for source, expected in (("github", {"sig_edge", "sig_new"}),
                             ("co", {"sig_entry"})):
        r = client.get(f"/signals?kind=changelog&source={source}"
                       "&since=2026-10-11T13:00:00Z", headers=_h())
        assert r.status_code == 200
        assert {s["id"] for s in r.json()["signals"]} == expected


@pytest.mark.parametrize("since", ["2026-10-11T13:00:00", "yesterday", ""])
def test_signals_refuses_a_since_that_is_not_a_zoned_timestamp(client, since):
    r = client.get(f"/signals?since={since}", headers=_h())
    assert r.status_code == 400
    assert r.json()["since"] == since


def test_signal_facets_applies_since(client):
    _seed_changelog()
    body = client.get("/signals/facets?kind=changelog&since=2026-10-12T00:00:00Z",
                      headers=_h()).json()
    assert body["total"] == 1


def test_produced_now_matches_the_stored_shape_since_compares_against(client):
    """CQ·207: the four write sites that stamp their own `produced_at`
    (`app._signal_course_status_anomaly` and the others) use `store.
    produced_now()` rather than `None`, and this is what ties that helper's
    output to `_since_clause`'s own contract — naive UTC text, seconds
    precision when there are no microseconds, `.%f` appended only when
    there are."""
    stamp = store.produced_now()
    parsed = _dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S.%f" if "." in stamp
                                   else "%Y-%m-%d %H:%M:%S")
    # Round-trips through `_since_clause`'s own bound shape: a bound equal to
    # the stamp itself is an inclusive match.
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (produced_at TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (stamp,))
    assert conn.execute(
        "SELECT 1 FROM t WHERE produced_at >= ?", (stamp,)).fetchone() is not None
    conn.close()
    assert abs((_dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
               - parsed).total_seconds()) < 5


def test_signals_refuses_an_unrecognised_parameter(client):
    r = client.get("/signals?stream=kb-audit&strem=kb-audit", headers=_h())
    assert r.status_code == 400
    assert r.json()["unknown"] == ["strem"]
    assert r.json()["supported"] == sorted(
        ["cursor", "kind", "limit", "offset", "order", "product", "repo",
         "since", "source", "stream", "unreferenced_by"])


def test_signals_default_order_is_ingested_at_descending(client):
    _seed_signal_filters()
    body = client.get("/signals", headers=_h()).json()
    ids = [s["id"] for s in body["signals"]]
    assert ids == ["sig_slack", "sig_ci", "sig_v1"]


def test_signals_order_produced_at_sorts_nulls_last(client):
    """`sig_ci` carries a `produced_at`; `sig_slack` and `sig_v1` do not, so
    they fall back to the `ingested_at DESC` tiebreak this order also names."""
    _seed_signal_filters()
    body = client.get("/signals?order=produced_at", headers=_h()).json()
    ids = [s["id"] for s in body["signals"]]
    assert ids == ["sig_ci", "sig_slack", "sig_v1"]


def test_signals_order_rejects_an_unknown_value_rather_than_falling_back(client):
    """A raw, unlisted value is a 400, not a silent read as the default —
    matching every other malformed-parameter refusal in this file."""
    r = client.get("/signals?order=body", headers=_h())
    assert r.status_code == 400
    assert "order" in r.json()["error"]


# --- unreferenced_by: the live-proposal join the service does, not the client ----


def _seed_proposals():
    """Three vivy proposals and the decisions that settle two of them.

    `prop_open` is settled by nothing. `prop_decided` is settled by a decision
    recording it at `refs.proposal`. `prop_other_key` is settled by one
    recording it at `refs.subject` instead — the convention drift the filter is
    built not to care about, since `kind` is an open vocabulary and a second
    writer can spell the key differently."""
    conn = _conn()
    conn.executemany(
        "INSERT INTO signals (id, product, source, kind, body, privacy_class, "
        "refs, ingested_at) VALUES (?,?,?,?,?,?,?,?)",
        [("prop_open", "vivy", "co", "proposal", "still live", "internal",
          None, "2026-08-02 01:00:00.000000"),
         ("prop_decided", "vivy", "co", "proposal", "approved", "internal",
          None, "2026-08-02 02:00:00.000000"),
         ("prop_other_key", "vivy", "co", "proposal", "declined", "internal",
          None, "2026-08-02 03:00:00.000000"),
         ("dec_1", "vivy", "co", "proposal_decision", "approve", "internal",
          '{"proposal": "prop_decided", "item": "issue_x"}',
          "2026-08-02 04:00:00.000000"),
         ("dec_2", "vivy", "co", "proposal_decision", "decline", "internal",
          '{"subject": "prop_other_key"}', "2026-08-02 05:00:00.000000")])
    conn.commit()
    conn.close()


def test_unreferenced_by_serves_only_the_live_proposals(client):
    """The whole point of the filter: Deck asks which proposals are still live
    and gets them, instead of fetching every proposal and every decision and
    joining them in `LibraryClient`."""
    _seed_proposals()
    body = client.get("/signals?kind=proposal&unreferenced_by=proposal_decision",
                      headers=_h()).json()
    assert [s["id"] for s in body["signals"]] == ["prop_open"]
    assert body["total"] == 1, "total has to agree with the page, or paging lies"


def test_unreferenced_by_does_not_depend_on_which_refs_key_carries_the_id(client):
    """`prop_other_key` is settled through `refs.subject` rather than
    `refs.proposal`. A key-path filter would return it as live — fewer rows than
    exist, looking like an answer — which is why this matches the id anywhere in
    `refs`."""
    _seed_proposals()
    ids = {s["id"] for s in client.get(
        "/signals?kind=proposal&unreferenced_by=proposal_decision",
        headers=_h()).json()["signals"]}
    assert "prop_other_key" not in ids


def test_unreferenced_by_omitted_leaves_every_proposal_listed(client):
    """The filter is opt-in. Without it the route answers exactly what it did
    before, which is what every existing caller depends on."""
    _seed_proposals()
    ids = {s["id"] for s in client.get("/signals?kind=proposal",
                                       headers=_h()).json()["signals"]}
    assert ids == {"prop_open", "prop_decided", "prop_other_key"}


def test_a_malformed_refs_blob_does_not_take_the_listing_down(client):
    """`json_each` over an unparseable blob raises `malformed JSON` for the
    WHOLE query, so without the `json_valid` guard one bad row anywhere in the
    table would 500 every listing that passes this filter — not just its own.
    `store._json_or` exists for the same reason on the read path."""
    _seed_proposals()
    conn = _conn()
    conn.execute(
        "INSERT INTO signals (id, product, source, kind, body, privacy_class, "
        "refs, ingested_at) VALUES (?,?,?,?,?,?,?,?)",
        ("dec_bad", "vivy", "co", "proposal_decision", "junk refs", "internal",
         "not json at all", "2026-08-02 06:00:00.000000"))
    conn.commit()
    conn.close()
    r = client.get("/signals?kind=proposal&unreferenced_by=proposal_decision",
                   headers=_h())
    assert r.status_code == 200
    assert [s["id"] for s in r.json()["signals"]] == ["prop_open"]


def test_a_decision_with_no_refs_settles_nothing(client):
    """The guard is a `CASE WHEN json_valid(...)`, and `json_valid(NULL)` is NULL
    rather than true, so a NULL `refs` takes the ELSE branch and matches nothing.
    That replaced an explicit `IS NOT NULL` test, so it is pinned here rather
    than left to the shape of the SQL."""
    _seed_proposals()
    conn = _conn()
    conn.execute(
        "INSERT INTO signals (id, product, source, kind, body, privacy_class, "
        "refs, ingested_at) VALUES (?,?,?,?,?,?,?,?)",
        ("dec_norefs", "vivy", "co", "proposal_decision", "no refs at all",
         "internal", None, "2026-08-02 08:00:00.000000"))
    conn.commit()
    conn.close()
    body = client.get("/signals?kind=proposal&unreferenced_by=proposal_decision",
                      headers=_h()).json()
    assert [s["id"] for s in body["signals"]] == ["prop_open"]


def test_a_decision_behind_the_customer_wall_does_not_hide_its_proposal(client):
    """A decision the caller may not read must not remove a proposal from the
    listing. Absence would say a decision exists, which is precisely what the
    wall refuses to say — so the subquery takes the same wall and the proposal
    reads as live to a caller that cannot see what settled it."""
    _seed_proposals()
    conn = _conn()
    conn.execute(
        "INSERT INTO signals (id, product, source, kind, body, privacy_class, "
        "refs, ingested_at) VALUES (?,?,?,?,?,?,?,?)",
        ("dec_cust", "vivy", "co", "proposal_decision", "customer-class",
         "customer", '{"proposal": "prop_open"}',
         "2026-08-02 07:00:00.000000"))
    conn.commit()
    conn.close()
    plain = client.get("/signals?kind=proposal&unreferenced_by=proposal_decision",
                       headers=_h()).json()
    assert "prop_open" in {s["id"] for s in plain["signals"]}
    priv = client.get("/signals?kind=proposal&unreferenced_by=proposal_decision",
                      headers=_h(SYSTEM)).json()
    assert "prop_open" not in {s["id"] for s in priv["signals"]}, (
        "a caller holding signal.customer sees the decision, so the proposal "
        "is settled for it")


def test_facets_applies_unreferenced_by_too(client):
    """The filter lives in the shared helper, so the aggregate cannot drift from
    the listing — the property that helper was written to hold."""
    _seed_proposals()
    body = client.get("/signals/facets?kind=proposal&unreferenced_by=proposal_decision",
                      headers=_h()).json()
    assert body["total"] == 1


# --- /signals/facets -------------------------------------------------------------


def test_signal_facets_is_registered_before_the_signal_id_route(client):
    """Starlette matches in registration order. The other way round, `facets`
    resolves as a signal id and this route is unreachable."""
    paths = [r.path for r in libapp.app.routes if getattr(r, "methods", None)]
    assert paths.index("/signals/facets") < paths.index("/signals/{signal_id}")


def test_signal_acks_is_registered_before_the_signal_id_route(client):
    paths = [r.path for r in libapp.app.routes if getattr(r, "methods", None)]
    assert paths.index("/signals/acks") < paths.index("/signals/{signal_id}")


def test_signal_facets_reports_total_embedded_and_by_source(client):
    _seed_signal_filters()
    body = client.get("/signals/facets", headers=_h()).json()
    # sig_v1, sig_ci, sig_slack are visible to a plain agent; sig_v2 is not.
    assert body["total"] == 3
    assert body["embedded"] == 1, "only sig_ci carries an embedded_at"
    by_source = {row[0]: row[1] for row in body["by_source"]}
    assert by_source["ci"] == 1
    assert by_source["slack"] == 1


def test_signal_facets_by_kind_reports_each_kind_with_its_true_count(client):
    """The issue's named regression: a kind in the fixture appears in
    `by_kind` WITH its correct count, so an implementation proving only that
    the field exists (an empty list) or that a kind is present (a count of
    one) still fails here — `build` is seeded twice. The customer-class build
    row is walled the same way `total` walls sig_v2, since a `by_kind` that
    counted it is the same disclosure as a `total` that does."""
    _seed_signal_filters()
    conn = _conn()
    conn.executemany(
        "INSERT INTO signals (id, product, source, kind, body, privacy_class, "
        "ingested_at) VALUES (?,?,?,?,?,?,?)",
        [("sig_ci2", "vivy", "ci", "build", "a second build", "internal",
          "2026-08-01 08:30:00.000000"),
         ("sig_ci3", "vivy", "ci", "build", "a customer build", "customer",
          "2026-08-01 08:31:00.000000")])
    conn.commit()
    conn.close()
    body = client.get("/signals/facets", headers=_h()).json()
    # Visible to a plain agent: sig_v1(note), sig_ci(build), sig_ci2(build),
    # sig_slack(chat). sig_v2 and sig_ci3 are customer-class. Descending by
    # count, then kind, per the route's shape contract.
    assert body["by_kind"] == [["build", 2], ["chat", 1], ["note", 1]]
    with_cap = client.get("/signals/facets", headers=_h(SYSTEM)).json()
    assert {row[0]: row[1] for row in with_cap["by_kind"]} == {
        "build": 3, "chat": 1, "note": 2}


def test_signal_facets_applies_the_kind_source_and_product_filters(client):
    _seed_signal_filters()
    body = client.get("/signals/facets?kind=build", headers=_h()).json()
    assert body["total"] == 1
    assert body["by_source"] == [["ci", 1]]
    assert body["by_kind"] == [["build", 1]], (
        "by_kind groups the kind-filtered set, not the whole table")


def test_signal_facets_total_excludes_a_customer_row_it_may_not_see(client):
    """The assertion the issue names by name: a `total` that counts a
    customer-class row is the same disclosure as listing it, so the customer
    wall has to hold in the aggregate the same way it holds in the list."""
    plain = client.get("/signals/facets", headers=_h()).json()
    assert plain["total"] == 1, "sig_v2 is customer-class and leaked into total"
    with_cap = client.get("/signals/facets", headers=_h(SYSTEM)).json()
    assert with_cap["total"] == 2, "system holds signal.customer and should see both"


def test_signal_facets_is_scoped_like_the_list_route(client):
    conn = _conn()
    conn.execute(
        "INSERT INTO signals (id, product, kind, body, privacy_class, "
        "ingested_at) VALUES (?,?,?,?,?,?)",
        ("sig_c1", "constance", "note", "a constance note", "internal",
         "2026-08-01 07:00:00.000000"))
    conn.commit()
    conn.close()
    scoped = client.get("/signals/facets", headers=_h()).json()
    assert scoped["total"] == 1 and scoped["products"] == ["vivy"]
    wide = client.get("/signals/facets", headers=_h(WIDE)).json()
    assert wide["total"] == 2, "sig_v2 stays walled even for a wildcard agent"
    assert set(wide["products"]) == {"vivy", "constance"}


def test_signal_facets_requires_signal_read(client, tmp_path, monkeypatch):
    toks = tmp_path / "narrow.json"
    toks.write_text(json.dumps({"narrow": {"name": "n", "products": ["vivy"]}}))
    monkeypatch.setattr(identity, "TOKENS", toks)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", tmp_path / "no-clients.json")
    monkeypatch.setattr(identity, "ROLE_CAPS", {"agent": frozenset({"item.read"})})
    r = client.get("/signals/facets", headers=_h("narrow"))
    assert r.status_code == 403 and "signal.read" in r.json()["error"]


def test_signal_facets_refuses_an_unrecognised_parameter(client):
    r = client.get("/signals/facets?order=kind", headers=_h())
    assert r.status_code == 400
    assert r.json()["unknown"] == ["order"]
    assert r.json()["supported"] == sorted(
        ["kind", "product", "repo", "since", "source", "stream",
         "unreferenced_by"])


def test_signal_facets_ordinary_request_still_succeeds(client):
    r = client.get("/signals/facets?kind=note&source=test&stream=kb-audit",
                   headers=_h())
    assert r.status_code == 200


# --- repo filter: meta.repo, scoped, repeatable like kind/source ---------------


def _seed_repo_signals():
    """`pr_state` rows the way the Keel's producer writes them: `meta.repo` as
    `owner/name`. Two rows for `convivy-deck`, one for `agent-platform`
    (distinguishing a single `repo` from the repeatable form), one under
    product `constance` for a repo that has no row anywhere in `vivy` (the
    cross-scope case), one customer-class `convivy-deck` row (the wall), and
    one row with no `repo` key at all in `meta` (the unfiltered-vs-filtered
    case)."""
    conn = _conn()
    conn.executemany(
        "INSERT INTO signals (id, product, source, kind, body, privacy_class, "
        "meta, ingested_at) VALUES (?,?,?,?,?,?,?,?)",
        [("sig_deck_1", "vivy", "keel", "pr_state", "deck pr 1", "internal",
          json.dumps({"repo": "convivy/convivy-deck"}),
          "2026-08-05 01:00:00.000000"),
         ("sig_deck_2", "vivy", "keel", "pr_state", "deck pr 2", "internal",
          json.dumps({"repo": "convivy/convivy-deck"}),
          "2026-08-05 02:00:00.000000"),
         ("sig_agent_platform", "vivy", "keel", "pr_state",
          "agent-platform pr", "internal",
          json.dumps({"repo": "convivy/agent-platform"}),
          "2026-08-05 03:00:00.000000"),
         ("sig_selector_other_product", "constance", "keel", "pr_state",
          "a selector pr, filed under constance", "internal",
          json.dumps({"repo": "convivy/selector"}),
          "2026-08-05 04:00:00.000000"),
         ("sig_deck_customer", "vivy", "keel", "pr_state",
          "customer-class deck row", "customer",
          json.dumps({"repo": "convivy/convivy-deck"}),
          "2026-08-05 05:00:00.000000"),
         ("sig_no_repo", "vivy", "keel", "pr_state", "no repo key at all",
          "internal", json.dumps({"pr": 42}),
          "2026-08-05 06:00:00.000000")])
    conn.commit()
    conn.close()


def test_repo_filter_returns_only_that_repos_rows(client):
    _seed_repo_signals()
    ids = {s["id"] for s in client.get(
        "/signals?repo=convivy/convivy-deck", headers=_h()).json()["signals"]}
    assert ids == {"sig_deck_1", "sig_deck_2"}


def test_repo_filter_is_repeatable(client):
    _seed_repo_signals()
    ids = {s["id"] for s in client.get(
        "/signals?repo=convivy/convivy-deck&repo=convivy/agent-platform",
        headers=_h()).json()["signals"]}
    assert ids == {"sig_deck_1", "sig_deck_2", "sig_agent_platform"}


def test_repo_filter_ands_with_kind_and_product(client):
    _seed_repo_signals()
    ids = {s["id"] for s in client.get(
        "/signals?kind=pr_state&product=vivy&repo=convivy/convivy-deck",
        headers=_h(WIDE)).json()["signals"]}
    assert ids == {"sig_deck_1", "sig_deck_2"}


def test_repo_filter_unknown_repo_is_an_empty_list_not_an_error(client):
    _seed_repo_signals()
    r = client.get("/signals?repo=convivy/does-not-exist", headers=_h())
    assert r.status_code == 200
    assert r.json()["signals"] == [] and r.json()["total"] == 0


def test_repo_filter_cannot_reach_across_scope(client):
    """`sig_selector_other_product` matches the repo but lives under
    `constance`; an identity scoped to `vivy` must get nothing, not the
    out-of-scope row."""
    _seed_repo_signals()
    body = client.get("/signals?repo=convivy/selector", headers=_h()).json()
    assert body["signals"] == [] and body["total"] == 0


def test_repo_filter_still_walls_a_customer_row(client):
    _seed_repo_signals()
    plain = {s["id"] for s in client.get(
        "/signals?repo=convivy/convivy-deck", headers=_h()).json()["signals"]}
    assert "sig_deck_customer" not in plain
    with_cap = {s["id"] for s in client.get(
        "/signals?repo=convivy/convivy-deck",
        headers=_h(SYSTEM)).json()["signals"]}
    assert "sig_deck_customer" in with_cap


def test_repo_filter_excludes_a_row_with_no_repo_key(client):
    _seed_repo_signals()
    unfiltered = {s["id"] for s in client.get(
        "/signals?kind=pr_state", headers=_h()).json()["signals"]}
    assert "sig_no_repo" in unfiltered, "unfiltered still returns it"
    filtered = {s["id"] for s in client.get(
        "/signals?kind=pr_state&repo=convivy/convivy-deck",
        headers=_h()).json()["signals"]}
    assert "sig_no_repo" not in filtered


def test_facets_honours_repo_the_same_way_the_listing_does(client):
    _seed_repo_signals()
    body = client.get("/signals/facets?repo=convivy/convivy-deck",
                      headers=_h()).json()
    assert body["total"] == 2


# --- pr-state latest: one row per PR, for Deck's panel -------------------------


def _pr_meta(**overrides):
    meta = {"repo": "convivy/vivy", "number": 1, "title": "a pr",
            "author": "someone", "draft": False, "head_sha": "sha-default",
            "state": "OPEN"}
    meta.update(overrides)
    return meta


def _insert_pr_state(conn, id_, product, meta, *, privacy_class="internal",
                     produced_at=None, ingested_at):
    conn.execute(
        "INSERT INTO signals (id, product, source, kind, body, "
        "privacy_class, meta, produced_at, ingested_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (id_, product, "keel", "pr_state", "a pr_state row", privacy_class,
         json.dumps(meta), produced_at, ingested_at))


def test_pr_state_returns_an_open_pr(client):
    conn = _conn()
    _insert_pr_state(conn, "sig_open1", "vivy", _pr_meta(number=1),
                     ingested_at=_window_safe_ts(0))
    conn.commit()
    conn.close()
    body = client.get("/signals/pr-state", headers=_h()).json()
    assert [p["number"] for p in body["prs"]] == [1]
    assert body["prs"][0]["state"] == "OPEN"


def test_pr_state_returns_a_closed_pr_inside_the_window(client):
    conn = _conn()
    _insert_pr_state(conn, "sig_closed1", "vivy",
                     _pr_meta(number=2, state="CLOSED"),
                     ingested_at=_window_safe_ts(5))
    conn.commit()
    conn.close()
    body = client.get("/signals/pr-state", headers=_h()).json()
    assert [p["number"] for p in body["prs"]] == [2]


def test_pr_state_excludes_a_closed_pr_outside_the_window(client):
    conn = _conn()
    _insert_pr_state(conn, "sig_closed_old", "vivy",
                     _pr_meta(number=3, state="CLOSED"),
                     ingested_at=_window_safe_ts(30))
    conn.commit()
    conn.close()
    body = client.get("/signals/pr-state", headers=_h()).json()
    assert body["prs"] == [] and body["total"] == 0


def test_pr_state_closed_within_days_rejects_a_non_integer(client):
    r = client.get("/signals/pr-state?closed_within_days=abc", headers=_h())
    assert r.status_code == 400


def test_pr_state_closed_within_days_rejects_out_of_range(client):
    """Both ends of the bound: below 0 and above the maximum. Either would be
    silently accepted if the chained comparison were loosened."""
    for bad in (-1, libapp.MAX_PR_STATE_WINDOW_DAYS + 1):
        r = client.get(f"/signals/pr-state?closed_within_days={bad}", headers=_h())
        assert r.status_code == 400, bad
    r = client.get(f"/signals/pr-state?closed_within_days={libapp.MAX_PR_STATE_WINDOW_DAYS}",
                   headers=_h())
    assert r.status_code == 200


def test_pr_state_refuses_an_unrecognised_parameter(client):
    r = client.get("/signals/pr-state?state=open", headers=_h())
    assert r.status_code == 400
    assert r.json()["unknown"] == ["state"]
    assert r.json()["supported"] == sorted(
        ["closed_within_days", "limit", "offset", "product", "repo"])


def test_pr_state_closed_within_days_moves_the_window(client):
    """A PR closed 10 days ago appears under the 14-day default, drops out of a
    7-day window, and appears again under 30 days."""
    conn = _conn()
    _insert_pr_state(conn, "sig_closed10", "vivy",
                     _pr_meta(number=7, state="CLOSED"),
                     ingested_at=_window_safe_ts(10))
    conn.commit()
    conn.close()
    def numbers(q=""):
        return [p["number"] for p in client.get(f"/signals/pr-state{q}", headers=_h()).json()["prs"]]
    assert numbers() == [7]
    assert numbers("?closed_within_days=7") == []
    assert numbers("?closed_within_days=30") == [7]


def test_pr_state_two_rows_returns_only_the_newer(client):
    conn = _conn()
    _insert_pr_state(conn, "sig_old_row", "vivy",
                     _pr_meta(number=4, head_sha="sha-old"),
                     ingested_at=_window_safe_ts(2))
    _insert_pr_state(conn, "sig_new_row", "vivy",
                     _pr_meta(number=4, head_sha="sha-new"),
                     ingested_at=_window_safe_ts(0))
    conn.commit()
    conn.close()
    body = client.get("/signals/pr-state", headers=_h()).json()
    assert len(body["prs"]) == 1
    assert body["prs"][0]["head_sha"] == "sha-new"
    assert body["prs"][0]["id"] == "sig_new_row"


def test_pr_state_tie_on_ingested_at_higher_id_wins(client):
    """Both rows share one `ingested_at` and a null `produced_at`. `id DESC`
    is the only thing that can break the tie — `sig_tie_b` sorts higher than
    `sig_tie_a`, so this fails if the tie-break is ever flipped to `id ASC`."""
    conn = _conn()
    ts = _window_safe_ts(0)
    _insert_pr_state(conn, "sig_tie_a", "vivy",
                     _pr_meta(number=5, head_sha="sha-a"), ingested_at=ts)
    _insert_pr_state(conn, "sig_tie_b", "vivy",
                     _pr_meta(number=5, head_sha="sha-b"), ingested_at=ts)
    conn.commit()
    conn.close()
    body = client.get("/signals/pr-state", headers=_h()).json()
    assert len(body["prs"]) == 1
    assert body["prs"][0]["id"] == "sig_tie_b"


def test_pr_state_produced_at_set_ranks_before_null(client):
    """`sig_no_produced` has the more recent `ingested_at` but a null
    `produced_at`; `sig_has_produced` has an older `ingested_at` but a set
    `produced_at`. The set one must win — `produced_at` outranks
    `ingested_at` whatever the two say about each other."""
    conn = _conn()
    _insert_pr_state(conn, "sig_no_produced", "vivy",
                     _pr_meta(number=6, head_sha="sha-null"),
                     ingested_at=_window_safe_ts(0))
    _insert_pr_state(conn, "sig_has_produced", "vivy",
                     _pr_meta(number=6, head_sha="sha-set"),
                     produced_at=_window_safe_ts(3),
                     ingested_at=_window_safe_ts(3))
    conn.commit()
    conn.close()
    body = client.get("/signals/pr-state", headers=_h()).json()
    assert len(body["prs"]) == 1
    assert body["prs"][0]["head_sha"] == "sha-set"


def test_pr_state_repo_filter_narrows_and_is_repeatable(client):
    conn = _conn()
    _insert_pr_state(conn, "sig_repo_a", "vivy",
                     _pr_meta(repo="convivy/vivy", number=10),
                     ingested_at=_window_safe_ts(0))
    _insert_pr_state(conn, "sig_repo_b", "vivy",
                     _pr_meta(repo="convivy/convivy-deck", number=11),
                     ingested_at=_window_safe_ts(0))
    _insert_pr_state(conn, "sig_repo_c", "vivy",
                     _pr_meta(repo="convivy/selector", number=12),
                     ingested_at=_window_safe_ts(0))
    conn.commit()
    conn.close()
    one = {p["id"] for p in client.get(
        "/signals/pr-state?repo=convivy/vivy", headers=_h()).json()["prs"]}
    assert one == {"sig_repo_a"}
    two = {p["id"] for p in client.get(
        "/signals/pr-state?repo=convivy/vivy&repo=convivy/convivy-deck",
        headers=_h()).json()["prs"]}
    assert two == {"sig_repo_a", "sig_repo_b"}


def test_pr_state_scoped_caller_sees_nothing_from_another_product(client):
    conn = _conn()
    _insert_pr_state(conn, "sig_constance_pr", "constance",
                     _pr_meta(number=20), ingested_at=_window_safe_ts(0))
    conn.commit()
    conn.close()
    body = client.get("/signals/pr-state", headers=_h()).json()
    assert body["prs"] == [] and body["total"] == 0


def test_pr_state_drops_pr_when_newest_row_is_out_of_scope(client):
    """`sig_scope_new`, the true latest row for PR 30, sits under `constance`
    — outside this AGENT token's `vivy`-only scope. The caller must not fall
    back to `sig_scope_old` and report it as latest; the whole PR must be
    absent."""
    conn = _conn()
    _insert_pr_state(conn, "sig_scope_old", "vivy",
                     _pr_meta(number=30, head_sha="sha-old-inscope"),
                     ingested_at=_window_safe_ts(2))
    _insert_pr_state(conn, "sig_scope_new", "constance",
                     _pr_meta(number=30, head_sha="sha-new-outscope"),
                     ingested_at=_window_safe_ts(0))
    conn.commit()
    conn.close()
    body = client.get("/signals/pr-state", headers=_h()).json()
    assert body["prs"] == []


def test_pr_state_customer_class_row_stays_walled(client):
    conn = _conn()
    _insert_pr_state(conn, "sig_customer_pr", "vivy", _pr_meta(number=40),
                     privacy_class="customer", ingested_at=_window_safe_ts(0))
    conn.commit()
    conn.close()
    plain = client.get("/signals/pr-state", headers=_h()).json()
    assert plain["prs"] == []
    privileged = client.get("/signals/pr-state", headers=_h(SYSTEM)).json()
    assert [p["id"] for p in privileged["prs"]] == ["sig_customer_pr"]


def test_pr_state_registered_before_signal_id_route():
    """Starlette matches in registration order. The other way round,
    `pr-state` resolves as a signal id and this route is dead code that
    looks alive, the same warning `facets`, `acks` and `search` carry."""
    paths = [r.path for r in libapp.app.routes if getattr(r, "methods", None)]
    assert paths.index("/signals/pr-state") < paths.index("/signals/{signal_id}")


def test_pr_state_path_does_not_resolve_as_a_signal_id(client):
    r = client.get("/signals/pr-state", headers=_h())
    assert r.status_code == 200, (
        "a 404 here means the path fell through to /signals/{signal_id} "
        "looking up a signal literally named 'pr-state'")
    assert "prs" in r.json()


def test_pr_state_pagination_totals(client):
    conn = _conn()
    for i in range(3):
        _insert_pr_state(conn, f"sig_page_{i}", "vivy",
                         _pr_meta(number=50 + i), ingested_at=_window_safe_ts(0))
    conn.commit()
    conn.close()
    first = client.get("/signals/pr-state?limit=2", headers=_h()).json()
    assert first["count"] == 2 and first["total"] == 3
    assert first["offset"] == 0 and first["truncated"] is True
    second = client.get(
        "/signals/pr-state?limit=2&offset=2", headers=_h()).json()
    assert second["count"] == 1 and second["total"] == 3
    assert second["truncated"] is False


# --- pr-state latest: the joined CI conclusion ---------------------------------


def _insert_test_result(conn, id_, product, *, commit, conclusion,
                        workflow="ci",
                        run_url="https://github.com/convivy/vivy/actions/runs/1",
                        privacy_class="internal", produced_at=None,
                        ingested_at):
    conn.execute(
        "INSERT INTO signals (id, product, source, kind, body, "
        "privacy_class, refs, produced_at, ingested_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (id_, product, "ci", "test-result", "a test-result row",
         privacy_class,
         json.dumps({"conclusion": conclusion, "workflow": workflow,
                     "commit": commit, "run_url": run_url}),
         produced_at, ingested_at))


def test_pr_state_joins_the_ci_conclusion_for_its_head_sha(client):
    conn = _conn()
    _insert_pr_state(conn, "sig_ci_pr", "vivy",
                     _pr_meta(number=60, head_sha="sha-ci-1"),
                     ingested_at=_window_safe_ts(0))
    _insert_test_result(conn, "sig_ci_result_1", "vivy", commit="sha-ci-1",
                        conclusion="success", workflow="e2e",
                        run_url="https://github.com/convivy/vivy/actions/runs/2",
                        ingested_at=_window_safe_ts(0))
    conn.commit()
    conn.close()
    pr = client.get("/signals/pr-state", headers=_h()).json()["prs"][0]
    assert pr["ci_conclusion"] == "success"
    assert pr["ci_workflow"] == "e2e"
    assert pr["ci_run_url"] == "https://github.com/convivy/vivy/actions/runs/2"
    assert pr["ci_captured"] is True


def test_pr_state_distinguishes_two_prs_with_different_ci_runs(client):
    conn = _conn()
    _insert_pr_state(conn, "sig_ci_pr_a", "vivy",
                     _pr_meta(number=61, head_sha="sha-ci-a"),
                     ingested_at=_window_safe_ts(0))
    _insert_pr_state(conn, "sig_ci_pr_b", "vivy",
                     _pr_meta(number=62, head_sha="sha-ci-b"),
                     ingested_at=_window_safe_ts(0))
    _insert_test_result(conn, "sig_ci_a", "vivy", commit="sha-ci-a",
                        conclusion="success", ingested_at=_window_safe_ts(0))
    _insert_test_result(conn, "sig_ci_b", "vivy", commit="sha-ci-b",
                        conclusion="failure", ingested_at=_window_safe_ts(0))
    conn.commit()
    conn.close()
    by_number = {p["number"]: p for p in
                client.get("/signals/pr-state", headers=_h()).json()["prs"]}
    assert by_number[61]["ci_conclusion"] == "success"
    assert by_number[62]["ci_conclusion"] == "failure"


def test_pr_state_no_matching_ci_row_is_explicit_absence_when_captured(client):
    """The product has a `test-result` row, just not for this commit —
    Deck's "checks have not run for this commit", not "this repo is not
    captured"."""
    conn = _conn()
    _insert_pr_state(conn, "sig_ci_no_match", "vivy",
                     _pr_meta(number=63, head_sha="sha-ci-nomatch"),
                     ingested_at=_window_safe_ts(0))
    _insert_test_result(conn, "sig_ci_other_commit", "vivy",
                        commit="sha-ci-elsewhere", conclusion="success",
                        ingested_at=_window_safe_ts(0))
    conn.commit()
    conn.close()
    pr = client.get("/signals/pr-state", headers=_h()).json()["prs"][0]
    assert pr["ci_conclusion"] is None
    assert pr["ci_workflow"] is None
    assert pr["ci_run_url"] is None
    assert pr["ci_captured"] is True


def test_pr_state_distinguishes_not_captured_from_no_result(client):
    """No `test-result` row exists anywhere for this PR's product — the
    Library captures no CI for it at all, the `unavailable` state Deck's
    cockpit already assumes for every PR today."""
    conn = _conn()
    _insert_pr_state(conn, "sig_ci_uncaptured", "vivy",
                     _pr_meta(number=64, head_sha="sha-ci-uncaptured"),
                     ingested_at=_window_safe_ts(0))
    conn.commit()
    conn.close()
    pr = client.get("/signals/pr-state", headers=_h()).json()["prs"][0]
    assert pr["ci_conclusion"] is None
    assert pr["ci_captured"] is False


def test_pr_state_ci_newest_result_wins_on_a_rerun(client):
    conn = _conn()
    _insert_pr_state(conn, "sig_ci_rerun_pr", "vivy",
                     _pr_meta(number=65, head_sha="sha-ci-rerun"),
                     ingested_at=_window_safe_ts(0))
    _insert_test_result(conn, "sig_ci_rerun_old", "vivy",
                        commit="sha-ci-rerun", conclusion="failure",
                        ingested_at=_window_safe_ts(2))
    _insert_test_result(conn, "sig_ci_rerun_new", "vivy",
                        commit="sha-ci-rerun", conclusion="success",
                        ingested_at=_window_safe_ts(0))
    conn.commit()
    conn.close()
    pr = client.get("/signals/pr-state", headers=_h()).json()["prs"][0]
    assert pr["ci_conclusion"] == "success"


def test_pr_state_ci_tie_on_ingested_at_higher_id_wins(client):
    """Both rows share one `ingested_at` and a null `produced_at` — the same
    tie `test_pr_state_tie_on_ingested_at_higher_id_wins` exercises for
    `pr_state` itself, here for the CI join's own ranking."""
    conn = _conn()
    ts = _window_safe_ts(0)
    _insert_pr_state(conn, "sig_ci_tie_pr", "vivy",
                     _pr_meta(number=66, head_sha="sha-ci-tie"),
                     ingested_at=ts)
    _insert_test_result(conn, "sig_ci_tie_a", "vivy", commit="sha-ci-tie",
                        conclusion="failure", ingested_at=ts)
    _insert_test_result(conn, "sig_ci_tie_b", "vivy", commit="sha-ci-tie",
                        conclusion="success", ingested_at=ts)
    conn.commit()
    conn.close()
    pr = client.get("/signals/pr-state", headers=_h()).json()["prs"][0]
    assert pr["ci_conclusion"] == "success", (
        "sig_ci_tie_b has the higher id and must win the tie")


def test_pr_state_ci_from_another_product_is_not_joined(client):
    """A `test-result` row for the same commit sha, but filed under a
    product outside this caller's scope, must not be joined — the caller
    sees the explicit absence, not a cross-scope conclusion."""
    conn = _conn()
    _insert_pr_state(conn, "sig_ci_scoped_pr", "vivy",
                     _pr_meta(number=67, head_sha="sha-ci-outscope"),
                     ingested_at=_window_safe_ts(0))
    _insert_test_result(conn, "sig_ci_outscope", "constance",
                        commit="sha-ci-outscope", conclusion="success",
                        ingested_at=_window_safe_ts(0))
    conn.commit()
    conn.close()
    pr = client.get("/signals/pr-state", headers=_h()).json()["prs"][0]
    assert pr["ci_conclusion"] is None
    assert pr["ci_captured"] is False, (
        "the constance row must not count as this vivy-scoped caller's own "
        "product being captured either")


def test_pr_state_ci_captured_is_keyed_on_repo_not_product(client):
    """`vivy` covers two repos (`convivy/vivy` and
    `convivy/vivy-ownerrez-sandbox-tools`, per `ci_tracked.py`'s
    `TRACKED`), and only `convivy/vivy` has ever had a `test-result` row.
    The sandbox repo's PR must read `ci_captured: false` — a captured
    sibling under the same product must not leak into it — while the
    `convivy/vivy` PR reads `ci_captured: true` even though the one
    captured row is for a different commit."""
    conn = _conn()
    _insert_pr_state(conn, "sig_ci_captured_repo", "vivy",
                     _pr_meta(repo="convivy/vivy", number=70,
                              head_sha="sha-ci-captured-repo"),
                     ingested_at=_window_safe_ts(0))
    _insert_pr_state(conn, "sig_ci_uncaptured_repo", "vivy",
                     _pr_meta(repo="convivy/vivy-ownerrez-sandbox-tools",
                              number=71, head_sha="sha-ci-uncaptured-repo"),
                     ingested_at=_window_safe_ts(0))
    _insert_test_result(
        conn, "sig_ci_captured_repo_row", "vivy",
        commit="sha-ci-captured-repo-elsewhere", conclusion="success",
        run_url="https://github.com/convivy/vivy/actions/runs/42",
        ingested_at=_window_safe_ts(0))
    conn.commit()
    conn.close()
    by_number = {p["number"]: p for p in
                client.get("/signals/pr-state", headers=_h()).json()["prs"]}
    assert by_number[70]["ci_conclusion"] is None
    assert by_number[70]["ci_captured"] is True
    assert by_number[71]["ci_captured"] is False, (
        "convivy/vivy-ownerrez-sandbox-tools shares vivy's product with "
        "convivy/vivy but has no test-result row of its own")


def test_pr_state_ci_captured_ignores_an_unparseable_run_url(client):
    """A `run_url` that isn't `https://github.com/<owner>/<repo>/actions/
    runs/<id>` names no repo at all, per `_repo_run_url_like`'s own
    docstring — it must not make any `(product, repo)` pair read as
    captured."""
    conn = _conn()
    _insert_pr_state(conn, "sig_ci_malformed_pr", "vivy",
                     _pr_meta(repo="convivy/vivy", number=72,
                              head_sha="sha-ci-malformed"),
                     ingested_at=_window_safe_ts(0))
    _insert_test_result(conn, "sig_ci_malformed_row", "vivy",
                        commit="sha-ci-malformed-elsewhere",
                        conclusion="success", run_url="not-a-github-url",
                        ingested_at=_window_safe_ts(0))
    conn.commit()
    conn.close()
    pr = client.get("/signals/pr-state", headers=_h()).json()["prs"][0]
    assert pr["ci_captured"] is False


def test_pr_state_ci_customer_class_row_stays_walled(client):
    """Mirrors `test_pr_state_customer_class_row_stays_walled` for the CI
    fields: a customer-class `test-result` row on a PR's own head sha must
    not populate `ci_conclusion`/`ci_workflow`/`ci_run_url`, and must not
    count toward `ci_captured`, for a caller lacking `signal.customer`."""
    conn = _conn()
    _insert_pr_state(conn, "sig_ci_wall_pr", "vivy",
                     _pr_meta(repo="convivy/vivy", number=73,
                              head_sha="sha-ci-wall"),
                     ingested_at=_window_safe_ts(0))
    _insert_test_result(
        conn, "sig_ci_wall_result", "vivy", commit="sha-ci-wall",
        conclusion="success", privacy_class="customer",
        run_url="https://github.com/convivy/vivy/actions/runs/7",
        ingested_at=_window_safe_ts(0))
    conn.commit()
    conn.close()
    plain = client.get("/signals/pr-state", headers=_h()).json()["prs"][0]
    assert plain["ci_conclusion"] is None
    assert plain["ci_workflow"] is None
    assert plain["ci_run_url"] is None
    assert plain["ci_captured"] is False
    privileged = client.get(
        "/signals/pr-state", headers=_h(SYSTEM)).json()["prs"][0]
    assert privileged["ci_conclusion"] == "success"
    assert privileged["ci_captured"] is True


# --- ci-capture: are CI results reaching the Archives for every tracked pair? --


def _ci_tracked(*pairs):
    return tuple(
        {"repo": repo, "workflow_file": workflow_file, "product": product}
        for repo, workflow_file, product in pairs)


@pytest.fixture
def ci_capture(client, monkeypatch):
    """`ci_tracked.TRACKED`/`NOT_CAPTURED` pinned to a small, test-owned
    fixture, so a `GET /signals/ci-capture` test never depends on this
    machine's real tracked-repo list."""
    monkeypatch.setattr(libapp.ci_tracked, "TRACKED", _ci_tracked(
        ("convivy/vivy", "ci.yml", "vivy"),
        ("convivy/hollis", "ci.yml", "vivy"),
    ))
    monkeypatch.setattr(libapp.ci_tracked, "NOT_CAPTURED", {})
    monkeypatch.setattr(libapp.ci_tracked, "NOT_CAPTURED_BY_NAME", {})
    return client


def test_ci_capture_never_captured_for_a_repo_with_zero_signals_not_for_one_with_some(
        ci_capture):
    """A tracked repo with no `test-result` signal at all appears in
    `never_captured`; a sibling that has one does not."""
    conn = _conn()
    _insert_test_result(conn, "sig_cap_has_one", "vivy",
                        commit="sha-cap-1", conclusion="success",
                        run_url="https://github.com/convivy/vivy/actions/runs/1",
                        ingested_at=_window_safe_ts(0))
    conn.commit()
    conn.close()
    body = ci_capture.get("/signals/ci-capture", headers=_h()).json()
    assert {t["repo"] for t in body["never_captured"]} == {"convivy/hollis"}
    tracked_by_repo = {t["repo"]: t for t in body["tracked"]}
    assert tracked_by_repo["convivy/vivy"]["test_result_count"] == 1
    assert tracked_by_repo["convivy/hollis"]["test_result_count"] == 0


def test_ci_capture_counts_each_workflow_of_one_repo_separately(
        ci_capture, monkeypatch):
    """`TRACKED` holds two `convivy/vivy` pairs, `ci.yml` and
    `e2e-stage-smoke.yml`. Each reports only its own workflow's rows, so a
    workflow with none is `never_captured` even though its sibling on the
    same repo has some. **Fails on the pre-fix code**, which counted by
    repo alone and gave both pairs the `ci.yml` row."""
    monkeypatch.setattr(libapp.ci_tracked, "TRACKED", _ci_tracked(
        ("convivy/vivy", "ci.yml", "vivy"),
        ("convivy/vivy", "e2e-stage-smoke.yml", "vivy"),
    ))
    conn = _conn()
    _insert_test_result(conn, "sig_cap_two_wf", "vivy",
                        commit="sha-cap-two-wf", conclusion="success",
                        workflow="ci",
                        run_url="https://github.com/convivy/vivy/actions/runs/3",
                        ingested_at=_window_safe_ts(0))
    conn.commit()
    conn.close()
    body = ci_capture.get("/signals/ci-capture", headers=_h()).json()
    by_workflow = {t["workflow_file"]: t for t in body["tracked"]}
    assert by_workflow["ci.yml"]["test_result_count"] == 1
    assert by_workflow["e2e-stage-smoke.yml"]["test_result_count"] == 0
    assert [t["workflow_file"] for t in body["never_captured"]] == [
        "e2e-stage-smoke.yml"]


def test_ci_capture_keeps_the_retired_fetcher_s_fields_at_their_absent_values(
        ci_capture):
    """The four fields that read `bin/ci_result_fetch.py`'s status file stay
    in the response under Library-Protocol 1, fixed at the values a client
    received whenever that file was missing (issue_01M2XGCSGYW6F0QEZAQ0VKN7CQ),
    even for a pair that has captured rows."""
    conn = _conn()
    _insert_test_result(conn, "sig_cap_retired_fields", "vivy",
                        commit="sha-cap-retired", conclusion="success",
                        run_url="https://github.com/convivy/vivy/actions/runs/2",
                        ingested_at=_window_safe_ts(1))
    conn.commit()
    conn.close()
    resp = ci_capture.get("/signals/ci-capture", headers=_h())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status_file"] == {"state": "absent"}
    assert body["quiet_this_run"] == []
    assert body["unreadable_last_run"] == []
    assert len(body["tracked"]) == 2
    assert all(t["last_fetch"] is None for t in body["tracked"])


def test_ci_capture_refuses_any_parameter(ci_capture):
    resp = ci_capture.get("/signals/ci-capture?repo=convivy/vivy", headers=_h())
    assert resp.status_code == 400
    assert resp.json()["unknown"] == ["repo"]
    assert resp.json()["supported"] == []


def test_ci_capture_lists_a_repo_deck_draws_that_is_neither_tracked_nor_explained(
        ci_capture, monkeypatch):
    monkeypatch.setattr(
        libapp.ci_tracked, "deck_tracked_repos",
        lambda: [{"repo": "convivy/vivy", "product": "vivy"},
                 {"repo": "convivy/mystery", "product": "vivy"}])
    body = ci_capture.get("/signals/ci-capture", headers=_h()).json()
    assert [u["repo"] for u in body["untracked"]] == ["convivy/mystery"]


def test_ci_capture_does_not_list_a_not_captured_repo_as_untracked(
        ci_capture, monkeypatch):
    monkeypatch.setattr(libapp.ci_tracked, "NOT_CAPTURED",
                        {"convivy/signals": "no GitHub Actions workflows"})
    monkeypatch.setattr(
        libapp.ci_tracked, "deck_tracked_repos",
        lambda: [{"repo": "convivy/vivy", "product": "vivy"},
                 {"repo": "convivy/signals", "product": "vivy"}])
    body = ci_capture.get("/signals/ci-capture", headers=_h()).json()
    assert body["untracked"] == []
    assert body["not_captured"] == [
        {"repo": "convivy/signals", "reason": "no GitHub Actions workflows"}]


def test_ci_capture_resolves_a_by_name_not_captured_entry_to_its_full_name(
        ci_capture, monkeypatch):
    """A `NOT_CAPTURED_BY_NAME` entry takes the owner `deck_tracked_repos()`
    gives it, is reported under that full name, and keeps that repository
    out of `untracked`."""
    monkeypatch.setattr(libapp.ci_tracked, "NOT_CAPTURED_BY_NAME",
                        {"side-project": "404 from the GitHub API"})
    monkeypatch.setattr(
        libapp.ci_tracked, "deck_tracked_repos",
        lambda: [{"repo": "convivy/vivy", "product": "vivy"},
                 {"repo": "example-owner/side-project", "product": "vivy"}])
    body = ci_capture.get("/signals/ci-capture", headers=_h()).json()
    assert body["untracked"] == []
    assert body["not_captured"] == [
        {"repo": "example-owner/side-project", "reason": "404 from the GitHub API"}]


@pytest.fixture
def ci_capture_multi_product(client, monkeypatch):
    """Every list `GET /signals/ci-capture` returns, carrying repos from TWO
    products — `AGENT` is scoped to `vivy` alone (see `_build_client`), so a
    fixture with every tracked, untracked and not-captured repo under that
    one product would pass a scope leak silently (reviewer and
    security-reviewer, PR #229 round 1: the original fixture did exactly
    that, which is why no test caught the gap `/signals/facets` had already
    fixed in PR #185).

    `convivy/orphan` names a `NOT_CAPTURED` repo `deck_tracked_repos()` does
    not yield at all, so its product cannot be determined — shown only to
    the wildcard caller.
    """
    monkeypatch.setattr(libapp.ci_tracked, "TRACKED", _ci_tracked(
        ("convivy/vivy", "ci.yml", "vivy"),
        ("convivy/constance", "ci.yml", "constance"),
    ))
    monkeypatch.setattr(libapp.ci_tracked, "NOT_CAPTURED", {
        "convivy/signals-vivy": "no GitHub Actions workflows",
        "convivy/signals-constance": "no GitHub Actions workflows",
        "convivy/orphan": "404 from the GitHub API",
    })
    monkeypatch.setattr(libapp.ci_tracked, "NOT_CAPTURED_BY_NAME", {})
    monkeypatch.setattr(
        libapp.ci_tracked, "deck_tracked_repos",
        lambda: [
            {"repo": "convivy/vivy", "product": "vivy"},
            {"repo": "convivy/constance", "product": "constance"},
            {"repo": "convivy/mystery-vivy", "product": "vivy"},
            {"repo": "convivy/mystery-constance", "product": "constance"},
            {"repo": "convivy/signals-vivy", "product": "vivy"},
            {"repo": "convivy/signals-constance", "product": "constance"},
        ])
    return client


def test_ci_capture_scoped_caller_sees_only_its_own_product_everywhere(
        ci_capture_multi_product):
    """A `vivy`-scoped caller (`AGENT`) must not see `constance`'s repo,
    workflow file or `NOT_CAPTURED` reason in any of the six lists — the
    disclosure both reviewers flagged on PR #229."""
    body = ci_capture_multi_product.get(
        "/signals/ci-capture", headers=_h(AGENT)).json()

    for key in ("tracked", "never_captured", "quiet_this_run",
               "unreadable_last_run"):
        repos = {t["repo"] for t in body[key]}
        assert "convivy/constance" not in repos, key
        products = {t["product"] for t in body[key]}
        assert "constance" not in products, key

    assert {t["repo"] for t in body["tracked"]} == {"convivy/vivy"}
    assert {u["repo"] for u in body["untracked"]} == {"convivy/mystery-vivy"}
    assert body["not_captured"] == [
        {"repo": "convivy/signals-vivy",
         "reason": "no GitHub Actions workflows"}]


def test_ci_capture_unscoped_caller_sees_every_product(
        ci_capture_multi_product):
    """`WIDE` carries `products: ["*"]` — the same identity every other
    scoped route treats as "every product", so this route must too."""
    body = ci_capture_multi_product.get(
        "/signals/ci-capture", headers=_h(WIDE)).json()

    assert {t["repo"] for t in body["tracked"]} == {
        "convivy/vivy", "convivy/constance"}
    assert {u["repo"] for u in body["untracked"]} == {
        "convivy/mystery-vivy", "convivy/mystery-constance"}
    assert {n["repo"] for n in body["not_captured"]} == {
        "convivy/signals-vivy", "convivy/signals-constance", "convivy/orphan"}


def test_ci_capture_not_captured_product_lookup_is_per_entry(
        ci_capture_multi_product):
    """`NOT_CAPTURED` maps a repo straight to a reason, with no product of
    its own — `convivy/signals-vivy`'s product comes from its matching
    `deck_tracked_repos()` entry, and that lookup must be per entry: the
    `vivy` reader sees its own repo and not `constance`'s, which proves the
    product was resolved rather than the whole section merely gated once on
    the caller's first-seen product."""
    vivy = ci_capture_multi_product.get(
        "/signals/ci-capture", headers=_h(AGENT)).json()
    assert {n["repo"] for n in vivy["not_captured"]} == {
        "convivy/signals-vivy"}


def test_ci_capture_not_captured_entry_with_no_known_product_is_wildcard_only(
        ci_capture_multi_product):
    """`convivy/orphan` is in `NOT_CAPTURED` but `deck_tracked_repos()` names
    no entry for it, so its product cannot be resolved — it must be shown
    only to a wildcard caller, exactly what `identity.in_scope(ident, None)`
    already returns."""
    scoped = ci_capture_multi_product.get(
        "/signals/ci-capture", headers=_h(AGENT)).json()
    assert "convivy/orphan" not in {n["repo"] for n in scoped["not_captured"]}

    wide = ci_capture_multi_product.get(
        "/signals/ci-capture", headers=_h(WIDE)).json()
    assert "convivy/orphan" in {n["repo"] for n in wide["not_captured"]}


def test_ci_capture_registered_before_signal_id_route():
    """Starlette matches in registration order. The other way round,
    `ci-capture` resolves as a signal id and this route is dead code that
    looks alive, the same warning `pr-state` carries."""
    paths = [r.path for r in libapp.app.routes if getattr(r, "methods", None)]
    assert paths.index("/signals/ci-capture") < paths.index("/signals/{signal_id}")


def test_ci_capture_path_does_not_resolve_as_a_signal_id(ci_capture):
    r = ci_capture.get("/signals/ci-capture", headers=_h())
    assert r.status_code == 200, (
        "a 404 here means the path fell through to /signals/{signal_id} "
        "looking up a signal literally named 'ci-capture'")
    assert "tracked" in r.json()


# --- the shadow ----------------------------------------------------------------


def test_the_connection_is_read_only(client):
    """THE SHADOW IS ENFORCED BY THE CONNECTION, NOT BY A PROMISE. The live
    database sits behind running pages and a decision plane, so "we only wrote
    reads" is not a claim worth resting on code review."""
    conn = store.connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE issue_items SET title = 'rewritten'")
    finally:
        conn.close()


def test_a_missing_database_raises_rather_than_reading_as_empty(monkeypatch, tmp_path):
    """An empty result set because the file was not found would report "nothing
    matches" on the strength of never having looked."""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "absent.db")
    with pytest.raises(FileNotFoundError):
        store.connect()


def test_an_unreadable_token_file_admits_nobody(monkeypatch, tmp_path):
    """Fail closed. A truncated or corrupted token file must not admit everyone."""
    monkeypatch.setattr(identity, "TOKENS", tmp_path / "absent.json")
    assert identity.resolve(f"Bearer {AGENT}") is None


def test_an_unrecognised_kind_resolves_as_agent_not_as_something_wider(tmp_path, monkeypatch):
    """A typo in the token file must not mint a role with more capability than
    intended."""
    toks = tmp_path / "t.json"
    toks.write_text(json.dumps({"tok": {"name": "x", "products": ["vivy"],
                                        "kind": "superuser"}}))
    monkeypatch.setattr(identity, "TOKENS", toks)
    ident = identity.resolve("Bearer tok")
    assert ident.kind == "agent"
    assert not identity.authorized(ident, "signal.customer")


def test_no_role_reachable_by_an_agent_can_decide(client):
    """ADR-0001's requirement is a capability an agent does not carry, at any
    protocol version — not a rule somebody remembers.

    **Derived from `ROLE_CAPS` rather than hand-listed** (reviewer, PR #49). The
    previous version enumerated `("agent", "system")`, so it silently stopped
    covering `approved_client` when that landed and would have stopped covering
    `customer_reader` too — a check that needs an edit every time a role is
    added is a check that will one day be a role behind. Inert today, since no
    role carries a `decide`-shaped capability after the decision plane was
    retired, and that is exactly when a weakening goes unnoticed."""
    for role in identity.ROLE_CAPS:
        assert "decide" not in identity.ROLE_CAPS[role], role


def test_the_role_table_names_only_roles_resolve_can_produce(tmp_path, monkeypatch):
    """A role nothing can reach is dead code that misleads in the dangerous
    direction: a maintainer granting `decide` to a `kind: human` token would get
    `agent` with no `decide` and no error (security-reviewer, PR #1).

    `approved_client` is reachable too, but through the separate client-token
    file (ADR-0038) rather than through `kind` in this one — so the fixture
    resolves one token from each file.

    **This is the test that catches a role added to the table and left
    unreachable**, which is why it grew a `customer_reader` entry rather than an
    exemption when that role landed, and an `orchestrator` one the same way when
    that role landed: the point is that every name in `ROLE_CAPS` has a token
    shape that produces it."""
    toks = tmp_path / "t.json"
    toks.write_text(json.dumps({
        "plain": {"name": "a", "products": ["vivy"]},
        "sys": {"name": "b", "products": ["vivy"], "kind": "system"},
        "reader": {"name": "d", "products": ["vivy"], "kind": "customer_reader"},
        "orch": {"name": "e", "products": ["vivy"], "kind": "orchestrator"},
        "wishful": {"name": "c", "products": ["vivy"], "kind": "human"},
    }))
    client_toks = tmp_path / "c.json"
    client_toks.write_text(json.dumps({
        "orient": {"name": "orient", "products": ["vivy"]},
    }))
    monkeypatch.setattr(identity, "TOKENS", toks)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", client_toks)
    produced = {identity.resolve(f"Bearer {t}").kind
                for t in ("plain", "sys", "reader", "orch", "wishful", "orient")}
    assert produced == set(identity.ROLE_CAPS), (
        "ROLE_CAPS names a role resolve() cannot produce, or vice versa")
    # And the wishful one is an agent, which is the fail-closed direction.
    assert identity.resolve("Bearer wishful").kind == "agent"


def test_zero_credentials_is_announced_as_a_warning(tmp_path, monkeypatch):
    """A token file this process cannot read refuses every request with a 401 and
    no other symptom. The startup line is the only place that is diagnosable."""
    monkeypatch.setattr(identity, "TOKENS", tmp_path / "absent.json")
    assert "WARNING" in identity.describe_credentials()
    assert "LIBRARY_API_TOKENS" in identity.describe_credentials()


# --- the Reference Room --------------------------------------------------------


@pytest.fixture
def kb(tmp_path, monkeypatch):
    """A knowledge index shaped like the real one: FTS5 over docs, no vectors."""
    db = tmp_path / "index.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE docs (
            id TEXT PRIMARY KEY, source TEXT, source_ref TEXT, kind TEXT,
            project TEXT, title TEXT, status TEXT, topics TEXT, audience TEXT,
            applies_to TEXT, depends_on TEXT, last_reviewed TEXT, authors TEXT,
            body TEXT, body_hash TEXT, notion_view INTEGER, updated_at TEXT);
        CREATE VIRTUAL TABLE docs_fts USING fts5(
            id, title, body, topics, content='docs', content_rowid='rowid');
    """)
    rows = [
        ("platform/decisions/0022-library-repo", "platform/decisions/0022.md",
         "decision", "platform", "The Library gets its own repository", "current",
         '["library","keel"]', "the Library is a service with its own storage"),
        ("vivy/specs/guest-stay", "projects/vivy/specs/guest-stay.md",
         "spec", "vivy", "Guest stay flow", "current",
         # Mentions the search term ONLY in its body, so BM25's column weighting
         # is actually exercised: the document carrying it in id and title must
         # outrank this one. Without a second match the ranking assertions pass
         # trivially on a single result.
         '["automations"]',
         "the guest stay materializer calls the library once for its light pairing"),
        ("platform/decisions/0009-retired", "platform/decisions/0009.md",
         "decision", "platform", "A retired decision about the library", "archived",
         '["library"]', "superseded reasoning about the library nobody should read"),
    ]
    for id_, ref, kind, proj, title, status, topics, body in rows:
        conn.execute(
            "INSERT INTO docs (id, source_ref, kind, project, title, status, topics, "
            "audience, body, last_reviewed, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            # An audience naming an agent role, so these are readable by an agent.
            (id_, ref, kind, proj, title, status, topics, '["team","coder"]', body,
             "2026-08-01", "2026-08-02"))
    # Two documents behind the audience wall: one whose audience names only humans,
    # one that declares none at all. Both carry the search term, so a filter that
    # is absent or applied after the query shows up immediately.
    conn.execute(
        "INSERT INTO docs (id, source_ref, kind, project, title, status, topics, "
        "audience, body, last_reviewed, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("company/legal/entity", "projects/company/legal/entity.md", "legal",
         "company", "Entity operations", "current", '["legal"]', '["team"]',
         "the library of filings, community property and tax detail",
         "2026-08-01", "2026-08-02"))
    conn.execute(
        "INSERT INTO docs (id, source_ref, kind, project, title, status, topics, "
        "audience, body, last_reviewed, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("platform/reference/unjudged", "projects/platform/reference/unjudged.md",
         "reference", "platform", "Nobody set an audience", "current",
         '["unjudged"]', None,
         "a library document nobody has judged for agent readability",
         "2026-08-01", "2026-08-02"))
    conn.execute("INSERT INTO docs_fts(rowid, id, title, body, topics) "
                 "SELECT rowid, id, title, body, topics FROM docs")
    # `body_hash` derived rather than left NULL: the real column is NOT NULL and
    # `doc_dict` serves it as `body_sha256`, so a fixture without it would let
    # every reference in this file assert against a null hash without saying so
    # (issue_01M19QQCKXW818SKY5A4G3XPHT).
    for doc_id, body in conn.execute("SELECT id, body FROM docs").fetchall():
        conn.execute("UPDATE docs SET body_hash = ? WHERE id = ?",
                     (hashlib.sha256((body or "").encode()).hexdigest(), doc_id))
    conn.commit()
    conn.close()
    monkeypatch.setattr(reference, "INDEX_DB", db)
    return db


def test_reference_search_ranks_and_paginates_like_every_other_list(client, kb):
    body = client.get("/reference/search?q=library", headers=_h()).json()
    assert body["count"] >= 1
    assert {"count", "total", "offset", "truncated"} <= set(body)
    # The BM25 weighting floats a doc whose ID and TITLE carry the term above one
    # that only mentions it in prose.
    assert body["documents"][0]["id"] == "platform/decisions/0022-library-repo"
    assert body["documents"][0]["source_ref"].endswith(".md")


def test_reference_search_hides_archived_unless_asked(client, kb):
    """A superseded document answering as though it were current is worse than no
    answer — the same reason a Course defaults to `live`."""
    default = client.get("/reference/search?q=library", headers=_h()).json()
    assert all(d["status"] != "archived" for d in default["documents"])
    asked = client.get("/reference/search?q=library&status=archived",
                       headers=_h()).json()
    assert [d["id"] for d in asked["documents"]] == ["platform/decisions/0009-retired"]


def test_reference_is_not_product_scoped_and_that_is_deliberate(client, kb):
    """A vivy-scoped agent MUST be able to read the platform's design rationale —
    the dispatch machinery points several repos at that corpus on purpose. This is
    the one read surface where scoping items the usual way would be wrong."""
    body = client.get("/reference/search?q=library", headers=_h()).json()
    assert any(d["project"] == "platform" for d in body["documents"])
    doc = client.get("/reference/documents/platform/decisions/0022-library-repo",
                     headers=_h())
    assert doc.status_code == 200
    assert "own storage" in doc.json()["body"]


def test_a_document_id_containing_slashes_resolves(client, kb):
    """Ids contain slashes — `platform/decisions/0022-...` is the id, not a route
    hierarchy. Without the path converter every real id would 404."""
    r = client.get("/reference/documents/platform/decisions/0022-library-repo",
                   headers=_h())
    assert r.status_code == 200 and r.json()["id"].count("/") == 2


def test_a_project_filter_narrows_without_being_a_boundary(client, kb):
    body = client.get("/reference/search?q=library&project=vivy",
                      headers=_h()).json()
    assert all(d["project"] == "vivy" for d in body["documents"])


def test_reference_search_filters_on_audience_like_its_listing_does(client, kb):
    """**THE LISTING FILTERED ON `audience` AND SEARCH IGNORED IT**, which is
    contract §3.2's case 1: the parameter was dropped, the caller got a 200 and
    every match, and nothing said the narrowing had not happened. Measured
    against the running service on 2026-08-26 before the fix — 416 documents
    with `audience=nonsense` and 416 without.

    The control is the second assertion. A corpus whose every row matched would
    pass this test with the WHERE clause deleted."""
    narrowed = client.get("/reference/search?q=library&audience=coder",
                          headers=_h()).json()
    assert narrowed["documents"], "the filter removed everything, so it proves nothing"
    assert all("coder" in (d["audience"] or []) for d in narrowed["documents"])
    unfiltered = client.get("/reference/search?q=library", headers=_h()).json()
    assert unfiltered["total"] > narrowed["total"], (
        "the audience filter removed no row, so it may not be applied at all")
    # The two rows it must remove, and they carry the query term: one whose
    # audience names only humans, one that declares none at all.
    ids = {d["id"] for d in narrowed["documents"]}
    assert "company/legal/entity" not in ids and "platform/reference/unjudged" not in ids


def test_the_audience_filter_narrows_the_total_and_not_just_the_page(client, kb):
    """**IN THE QUERY, NOT AFTER IT.** `total` is what a caller pages against, so
    a filter applied to the page alone would report more documents than the
    caller can reach and page into empty results.

    `/signals/search` cannot make this promise — it filters an oversampled KNN
    window — which is why that route reports no `total` at all."""
    narrowed = client.get("/reference/search?q=library&audience=coder&limit=50",
                          headers=_h()).json()
    unfiltered = client.get("/reference/search?q=library&limit=50",
                            headers=_h()).json()
    # **BOTH HALVES, OR THIS PROVES NOTHING.** `total == len(documents)` holds
    # trivially whenever the corpus fits one page, so it passes just as happily
    # with the filter deleted. The comparison against the unfiltered total is
    # what distinguishes a narrowed count from an unnarrowed one.
    assert narrowed["total"] == len(narrowed["documents"])
    assert narrowed["total"] < unfiltered["total"]


def test_an_unmatched_audience_narrows_to_nothing_rather_than_everything(client, kb):
    """A wider answer reads as a working call; this is the shape that hid the
    defect for as long as it existed."""
    body = client.get("/reference/search?q=library&audience=nobody-has-this",
                      headers=_h()).json()
    assert body["total"] == 0 and body["documents"] == []


def test_a_malformed_audience_row_does_not_500_reference_search(client, kb):
    """The same guard the listing carries, now reached through the shared clause.
    `json_each` raises `malformed JSON` on an unparseable value, and one such row
    would take down the route for every caller rather than for that document.
    The knowledge repository writes this column, so its content is not ours to
    guarantee."""
    conn = sqlite3.connect(kb)
    conn.execute(
        "INSERT INTO docs (id, source_ref, kind, project, title, status, topics, "
        "audience, body, last_reviewed, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("platform/specs/broken", "projects/platform/specs/broken.md", "spec",
         "platform", "Broken audience", "current", '["library"]', "not-json",
         "a library document with an unparseable audience",
         "2026-08-01", "2026-08-02"))
    conn.execute("INSERT INTO docs_fts(rowid, id, title, body, topics) "
                 "SELECT rowid, id, title, body, topics FROM docs "
                 "WHERE id = 'platform/specs/broken'")
    conn.commit()
    conn.close()
    unfiltered = client.get("/reference/search?q=library", headers=_h())
    assert unfiltered.status_code == 200
    assert "platform/specs/broken" in {d["id"] for d in unfiltered.json()["documents"]}, (
        "the bad row is not in the corpus, so the filtered call below proves nothing")
    r = client.get("/reference/search?q=library&audience=coder", headers=_h())
    assert r.status_code == 200
    assert "platform/specs/broken" not in {d["id"] for d in r.json()["documents"]}


def test_an_all_stopword_query_returns_nothing_and_says_why(client, kb):
    """Returning the whole corpus unranked is the worse failure."""
    body = client.get("/reference/search?q=how do we", headers=_h()).json()
    assert body["documents"] == [] and body["total"] == 0
    assert "stopword" in body["note"]


def test_fts_operators_in_a_query_are_quoted_rather_than_parsed(client, kb):
    """A malformed MATCH is a 500 on a read path, and a cleverly-formed one is a
    query the caller did not write."""
    for q in ("library AND NOT keel", 'library" OR "', "library NEAR/2 keel"):
        r = client.get("/reference/search", params={"q": q}, headers=_h())
        assert r.status_code == 200, f"{q!r} broke the MATCH expression"


def test_reference_search_requires_a_query(client, kb):
    r = client.get("/reference/search", headers=_h())
    assert r.status_code == 400 and "q is required" in r.json()["error"]


def test_reference_search_refuses_an_unrecognised_parameter(client, kb):
    r = client.get("/reference/search?q=library&sort=newest", headers=_h())
    assert r.status_code == 400
    assert r.json()["unknown"] == ["sort"]
    assert r.json()["supported"] == sorted(
        ["audience", "kind", "limit", "offset", "project", "q", "status"])


def test_topics_carry_their_document_counts(client, kb):
    """A topic on one document and a topic on ninety are different facts."""
    body = client.get("/reference/topics", headers=_h()).json()
    topics = {t["topic"]: t["documents"] for t in body["topics"]}
    assert topics["library"] == 1, "archived documents leaked into the vocabulary"
    assert "automations" in topics


def test_reference_topics_refuses_any_parameter(client, kb):
    r = client.get("/reference/topics?sort=newest", headers=_h())
    assert r.status_code == 400
    assert r.json()["unknown"] == ["sort"]
    assert r.json()["supported"] == []


def test_the_reference_routes_need_their_own_capability(client, kb, tmp_path, monkeypatch):
    toks = tmp_path / "narrow.json"
    toks.write_text(json.dumps({"narrow": {"name": "n", "products": ["vivy"]}}))
    monkeypatch.setattr(identity, "TOKENS", toks)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", tmp_path / "no-clients.json")
    monkeypatch.setattr(identity, "ROLE_CAPS", {"agent": frozenset({"item.read"})})
    r = client.get("/reference/search?q=library", headers=_h("narrow"))
    assert r.status_code == 403 and "reference.read" in r.json()["error"]


def test_search_returns_a_score_and_higher_means_more_relevant(client, kb):
    """The contract promises "ranked document matches for a query, WITH SCORES".
    Ordering alone gives a caller nothing to threshold on (reviewer, PR #2)."""
    body = client.get("/reference/search?q=library", headers=_h()).json()
    docs = body["documents"]
    assert all("score" in d for d in docs), "the contract's score is missing"
    # Descending, because score_of negates FTS5's negative-is-better bm25 — a raw
    # value under the name `score` would invite a threshold that selects the WORST
    # matches.
    assert docs == sorted(docs, key=lambda d: -d["score"])
    assert docs[0]["score"] > 0, (
        "a score rounded to a fixed decimal place collapses to 0.0 on a small "
        "corpus — BM25's magnitude scales with the index")
    assert len(docs) >= 2, "ranking cannot be asserted on a single result"
    assert docs[0]["score"] > docs[-1]["score"]
    # The document carrying the term in its id and title outranks the one that
    # only mentions it in prose — which is what the column weighting is for.
    assert docs[0]["id"] == "platform/decisions/0022-library-repo"


def test_the_ranking_constants_have_not_drifted(): 
    """Two search surfaces over one index that rank differently would make "the KB
    says" depend on which door you came through.

    **Two halves, and only the first runs in CI.** The literals below fail the
    moment THIS copy changes. The cross-repo comparison needs an agent-platform
    checkout, which CI does not have — the same invisible-drift shape that let
    `KNOWN_KB_PROJECTS` and orient's registry test rot for two days each. It fails
    locally and on the mini, which is where anyone editing either file works."""
    assert reference.BM25_WEIGHTS == "10.0, 8.0, 1.0, 4.0"
    assert len(reference.STOPWORDS) == 86
    assert {"the", "how", "please"} <= reference.STOPWORDS
    assert "deploy" not in reference.STOPWORDS and "setup" not in reference.STOPWORDS

    source = pathlib.Path.home() / "Convivy/agent-platform/bin/mcp-kb-server"
    if not source.is_file():
        pytest.skip("agent-platform not checked out; the cross-repo half cannot run")
    text = source.read_text()
    weights = re.search(r'_BM25_WEIGHTS\s*=\s*"([^"]+)"', text)
    assert weights and weights.group(1) == reference.BM25_WEIGHTS, (
        "BM25 weights have drifted from mcp-kb-server")
    stop = re.search(r'_STOPWORDS\s*=\s*frozenset\("""(.*?)"""', text, re.S)
    assert stop and frozenset(stop.group(1).split()) == reference.STOPWORDS, (
        "the stopword list has drifted from mcp-kb-server")


def test_the_tokenizer_not_the_quoting_is_what_stops_an_operator(client, kb):
    """A term can never contain a quote or an FTS5 operator character, because the
    tokenizer strips them before quoting happens (reviewer, PR #2)."""
    assert reference.build_match('foo"bar') == '"foo" OR "bar"'
    assert reference.build_match("café") == '"caf"'
    # Every token here is a single character, and single characters are dropped
    # before quoting — so the operators never reach an expression at all.
    assert reference.build_match("a*b^c:d(e)") is None
    assert reference.build_match("lib*rary^keel") == '"lib" OR "rary" OR "keel"'

    # And a term that IS a bare keyword is what the quoting exists for.
    assert reference.build_match("near keel") == '"near" OR "keel"'


# --- the audience field, returned and not enforced ------------------------------


def test_a_human_only_document_is_readable_and_carries_its_audience(client, kb):
    """**A wall was built here and DENIED** (the operator, 2026-08-08,
    `dec_01KZH8XTJWXK5SV2NJBCD0J3C1`). This pins the decided posture rather than
    the absence of code, so a future change that quietly re-adds enforcement fails
    a test that names the decision instead of looking like an improvement.

    The condition that reverses it is parked as `issue_01KZHD2AHS6MB4ZGYQFGPZPD2R`:
    go-to-market, or any access that is not the operator's own."""
    r = client.get("/reference/documents/company/legal/entity", headers=_h())
    assert r.status_code == 200
    assert r.json()["audience"] == ["team"], (
        "the audience travels on the document so a caller can respect it")


def test_a_document_with_no_audience_is_readable_too(client, kb):
    body = client.get("/reference/search?q=library", headers=_h()).json()
    assert "platform/reference/unjudged" in [d["id"] for d in body["documents"]]


def test_the_topic_vocabulary_covers_every_readable_document(client, kb):
    body = client.get("/reference/topics", headers=_h()).json()
    topics = {t["topic"] for t in body["topics"]}
    assert {"library", "legal", "unjudged"} <= topics


# --- semantic search over the Archives -----------------------------------------


@pytest.fixture
def vec(client, tmp_path, monkeypatch):
    """A vector index over the fixture's signals, with a deterministic embedder.

    **No ONNX model is downloaded to assert that a scope filter works.** The
    embedder is a seam for exactly this: a stub that maps a word to a fixed vector
    makes the KNN's ORDER predictable, which is what these tests are about."""
    sqlite_vec = pytest.importorskip("sqlite_vec")
    conn = sqlite3.connect(store.DB_PATH)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute(f"CREATE VIRTUAL TABLE {vectors.VEC_TABLE} USING "
                 f"vec0(embedding float[{vectors.EMBED_DIM}], vault text, +item_id text)")
    # A constance signal, so the scope wall has something to remove.
    conn.execute("INSERT INTO signals (id, product, kind, body, privacy_class, "
                 "ingested_at) VALUES (?,?,?,?,?,?)",
                 ("sig_c1", "constance", "note", "a constance note", "internal",
                  "2026-08-03"))

    def vector_for(seed: float) -> bytes:
        import struct
        return struct.pack(f"{vectors.EMBED_DIM}f",
                           *([seed] + [0.0] * (vectors.EMBED_DIM - 1)))

    # Distances ascend in this order, so the nearest row is the vivy internal one
    # and the walled rows sit behind it — a filter applied after the KNN rather
    # than to the joined rows would still return them.
    for sid, seed in (("sig_v1", 0.0), ("sig_v2", 0.1), ("sig_c1", 0.2)):
        conn.execute(
            f"INSERT INTO {vectors.VEC_TABLE}(embedding, vault, item_id) VALUES (?,?,?)",
            (vector_for(seed), vectors.VAULT_ARCHIVES, sid))
    conn.commit()
    conn.close()
    monkeypatch.setattr(vectors, "_embedder",
                        lambda texts: [[0.0] * vectors.EMBED_DIM])
    return client


def test_semantic_search_returns_nearest_first_with_a_distance(vec):
    body = vec.get("/signals/search?q=anything", headers=_h()).json()
    assert body["signals"][0]["id"] == "sig_v1"
    assert "distance" in body["signals"][0]
    assert body["query"] == "anything"


def test_semantic_search_is_not_a_side_door_around_product_scope(vec):
    """A caller that cannot LIST a row must not learn it exists through a
    similarity score. The constance signal is nearer than nothing and must not
    appear for a vivy-scoped caller."""
    body = vec.get("/signals/search?q=anything", headers=_h()).json()
    assert "sig_c1" not in [s["id"] for s in body["signals"]]
    wide = vec.get("/signals/search?q=anything", headers=_h(WIDE)).json()
    assert "sig_c1" in [s["id"] for s in wide["signals"]], (
        "the fixture's cross-product row is unreachable even to a wildcard caller, "
        "so the scope assertion above proves nothing")


def test_semantic_search_is_not_a_side_door_around_the_customer_wall(vec):
    """The same rule for customer-authored content, which the list route already
    hides — a vector index must not reintroduce it."""
    body = vec.get("/signals/search?q=anything", headers=_h()).json()
    assert "sig_v2" not in [s["id"] for s in body["signals"]]
    allowed = vec.get("/signals/search?q=anything", headers=_h(SYSTEM)).json()
    assert "sig_v2" in [s["id"] for s in allowed["signals"]]


def test_semantic_search_requires_a_query(vec):
    r = vec.get("/signals/search", headers=_h())
    assert r.status_code == 400 and "q is required" in r.json()["error"]


def test_search_is_registered_before_the_id_route(client):
    """Starlette matches in registration order. The other way round, `search`
    resolves as a signal id and this route is dead code that looks alive."""
    paths = [r.path for r in libapp.app.routes if getattr(r, "methods", None)]
    assert paths.index("/signals/search") < paths.index("/signals/{signal_id}")


def test_an_unavailable_index_refuses_with_a_reason_rather_than_a_stack_trace(client, monkeypatch):
    """A 500 from a missing extension tells a caller nothing it can act on."""
    monkeypatch.setattr(vectors, "available",
                        lambda _p=None: (False, "sqlite-vec is not installed"))
    r = client.get("/signals/search?q=anything", headers=_h())
    assert r.status_code == 503
    assert "sqlite-vec" in r.json()["error"]
    assert "unaffected" in r.json()["hint"]


def test_an_embedder_of_the_wrong_dimension_raises_rather_than_returning_noise(monkeypatch):
    """A dimension mismatch does not error in vec0, it silently returns nonsense
    neighbours — which is why it is asserted on every embed."""
    monkeypatch.setattr(vectors, "_embedder", lambda texts: [[0.0] * 128])
    with pytest.raises(ValueError, match="dimensions"):
        vectors.embed_query("anything")


def test_semantic_search_honours_offset(vec):
    """An earlier version validated `offset` and discarded it, so every page was
    the first one and nothing said so (reviewer, PR #2)."""
    first = vec.get("/signals/search?q=anything&limit=1", headers=_h(WIDE)).json()
    second = vec.get("/signals/search?q=anything&limit=1&offset=1",
                     headers=_h(WIDE)).json()
    assert first["signals"][0]["id"] != second["signals"][0]["id"]
    assert second["offset"] == 1


def test_a_short_page_says_whether_more_exists(vec):
    """"Few matches" and "the window filtered down to few" look identical to a
    caller unless the response distinguishes them."""
    body = vec.get("/signals/search?q=anything&limit=1", headers=_h(WIDE)).json()
    assert body["truncated"] is True, "more rows survived past this page"
    # The wildcard caller sees two of the three vectors — the third is
    # customer-class and behind its own wall — so offset 1 is the last page.
    last = vec.get("/signals/search?q=anything&limit=1&offset=1",
                   headers=_h(WIDE)).json()
    assert last["count"] == 1 and last["truncated"] is False
    past = vec.get("/signals/search?q=anything&limit=1&offset=5",
                   headers=_h(WIDE)).json()
    assert past["count"] == 0 and past["truncated"] is False


def _index_sourced_signals():
    """Two more vivy signals carrying DIFFERENT sources, vectorised so the search
    window holds both. The base `vec` fixture's rows carry no source at all, so a
    source filter over them alone could never remove anything and every assertion
    about it would pass with the clause deleted."""
    import struct
    sqlite_vec = pytest.importorskip("sqlite_vec")
    conn = sqlite3.connect(store.DB_PATH)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executemany(
        "INSERT INTO signals (id, product, source, kind, body, privacy_class, "
        "ingested_at) VALUES (?,?,?,?,?,?,?)",
        [("sig_ci2", "vivy", "ci", "build", "a ci row", "internal",
          "2026-08-04 08:00:00.000000"),
         ("sig_sl2", "vivy", "slack", "chat", "a slack row", "internal",
          "2026-08-04 09:00:00.000000")])
    for sid, seed in (("sig_ci2", 0.3), ("sig_sl2", 0.4)):
        conn.execute(
            f"INSERT INTO {vectors.VEC_TABLE}(embedding, vault, item_id) VALUES (?,?,?)",
            (struct.pack(f"{vectors.EMBED_DIM}f",
                         *([seed] + [0.0] * (vectors.EMBED_DIM - 1))),
             vectors.VAULT_ARCHIVES, sid))
    conn.commit()
    conn.close()


def test_semantic_search_honours_source(vec):
    """**IT TOOK THE PARAMETER AND NEVER READ IT** — the agent-facing
    `signal_search` tool advertised `source` and forwarded it here, so a caller
    narrowing to one source got a 200, plausible rows, and no narrowing
    (issue_01M0ZXSVZG8K9DGG2CSX0JYE5K). A working filter and an ignored one
    returned the same evidence.

    The second assertion is the control: without it, a corpus where every row
    matched would pass with the clause deleted."""
    _index_sourced_signals()
    body = vec.get("/signals/search?q=anything&limit=20&source=ci",
                   headers=_h()).json()
    assert [s["id"] for s in body["signals"]] == ["sig_ci2"]
    unfiltered = vec.get("/signals/search?q=anything&limit=20", headers=_h()).json()
    assert "sig_sl2" in [s["id"] for s in unfiltered["signals"]], (
        "the row the filter must remove is not in the window, so the "
        "assertion above proves nothing")


def test_semantic_search_source_is_repeatable_like_the_list_route(vec):
    """`GET /signals` reads `source` with `getlist`, so `?source=a&source=b` means
    `IN (a, b)` there. One parameter must not mean two things across the two
    routes — Starlette's `get` would silently keep the LAST value and halve the
    caller's intent, which contract §3.2 records as the live hazard on the
    documents route."""
    _index_sourced_signals()
    body = vec.get("/signals/search?q=anything&limit=20&source=ci&source=slack",
                   headers=_h()).json()
    assert {s["id"] for s in body["signals"]} == {"sig_ci2", "sig_sl2"}


def test_semantic_search_refuses_kind_rather_than_ignoring_it(vec):
    """**REFUSED, NOT IMPLEMENTED, AND NOT DROPPED.** Filters on this route run
    after an oversampled KNN, so `?kind=alert` over a class holding 110 of 21,110
    rows would answer an empty page with `truncated: false` — "none in the
    nearest hundred" reading as "none exist". Silently ignoring it is the same
    wrong answer with nothing that reports it."""
    _index_sourced_signals()
    r = vec.get("/signals/search?q=anything&kind=build", headers=_h())
    assert r.status_code == 400
    assert "kind" in r.json()["error"]
    assert "/signals" in r.json()["hint"]


def test_the_route_the_kind_refusal_names_actually_filters_by_kind(vec):
    """The refusal is only useful if the alternative it names works. A hint
    pointing at a route that ignored `kind` too would read as help and be a dead
    end."""
    _index_sourced_signals()
    body = vec.get("/signals?kind=build", headers=_h()).json()
    assert [s["id"] for s in body["signals"]] == ["sig_ci2"]
    assert body["total"] == 1


def test_semantic_search_refuses_an_unrecognised_parameter(vec):
    r = vec.get("/signals/search?q=anything&stream=kb-audit", headers=_h())
    assert r.status_code == 400
    assert r.json()["unknown"] == ["stream"]
    assert r.json()["supported"] == sorted(
        ["kind", "limit", "offset", "product", "q", "source"])


def test_semantic_search_reports_no_total_and_that_is_deliberate(vec):
    """"How many signals are semantically near this query" has no answer — every
    vector has some distance — so a total would be an arbitrary window size dressed
    up as a fact."""
    body = vec.get("/signals/search?q=anything", headers=_h()).json()
    assert "truncated" in body and "offset" in body
    assert "total" not in body


def test_a_full_knn_window_does_not_leak_truncation_to_a_scoped_caller(vec, monkeypatch):
    """`truncated` is POST-FILTER ONLY, and this is the leak that decided it
    (security-reviewer, PR #2).

    The raw candidate count is identical for every caller whatever their scope, so
    folding it in would let a restricted caller read a short page plus
    `truncated: true` and infer that near matches exist beyond its scope — the
    inference `list_signals` computes its `total` post-filter to prevent.

    Oversample of 1 makes the window exactly `limit`, so the KNN returns its whole
    window while this caller's own survivors are exhausted."""
    monkeypatch.setattr(vectors, "OVERSAMPLE", 1)
    body = vec.get("/signals/search?q=anything&limit=1", headers=_h()).json()
    assert body["count"] == 1
    assert body["truncated"] is False, (
        "a pre-filter fact reached a scoped caller through `truncated`")


def test_a_filtered_page_the_window_emptied_does_not_claim_completeness(vec, monkeypatch):
    """**THE REPORTED DEFECT** (issue_01M11WR1ZHSDSBZAE1HTZS6P6Z): `?q=deploy&
    source=deploy_watch` answered `count: 0, truncated: false` while 11
    deploy_watch rows sat in the table. The filter runs after the KNN, so it
    removed a window that never held the source, and the envelope reported that
    page as everything.

    Oversample of 1 makes the window exactly `limit`, so `sig_ci2` is a row that
    exists and is outside it — the rare-source case, reproduced.

    **`count == 0` is asserted by both the broken and the fixed code**, so it is
    the `filtered_out` assertion that carries this test. The two below it are the
    controls: without the first, the source might match nothing anywhere; without
    the second, the window might have been empty for an unrelated reason."""
    _index_sourced_signals()
    monkeypatch.setattr(vectors, "OVERSAMPLE", 1)
    body = vec.get("/signals/search?q=anything&limit=1&source=ci",
                   headers=_h()).json()
    assert body["count"] == 0
    assert body["filtered_out"] == 1, (
        "an empty filtered page reported nothing about what its own filter "
        "removed, so it reads as complete")

    listed = vec.get("/signals?source=ci", headers=_h()).json()
    assert [s["id"] for s in listed["signals"]] == ["sig_ci2"], (
        "the row the search could not reach does not exist, so the assertion "
        "above is about an empty table rather than a window")
    unfiltered = vec.get("/signals/search?q=anything&limit=1",
                         headers=_h()).json()
    assert unfiltered["count"] == 1 and unfiltered["filtered_out"] == 0, (
        "the window was empty for this caller anyway, so the filter is not "
        "what emptied the page")


def test_filtered_out_separates_a_window_a_filter_emptied_from_one_that_was_empty(vec, monkeypatch):
    """**THE TWO STATES A CALLER HAS TO TELL APART**, and the reason asserting the
    envelope alone proves nothing: `count: 0` is the same in both.

    A non-zero `filtered_out` means the search found rows for this caller and the
    caller's own filter removed them — inconclusive, ask `GET /signals`. A zero
    means nothing was near at all, which is the answer. The second state must
    stay ambiguous as to WHY it is empty, because the scope wall's removals are
    counted on the other side of it."""
    _index_sourced_signals()
    monkeypatch.setattr(vectors, "OVERSAMPLE", 1)
    emptied = vec.get("/signals/search?q=anything&limit=1&source=ci",
                      headers=_h()).json()
    # Scoped to no product, so the window survives the KNN and nothing survives
    # the wall — the state where the response genuinely has nothing to report.
    nothing_near = vec.get("/signals/search?q=anything&limit=1&source=ci",
                           headers=_h(NOSCOPE)).json()
    assert emptied["count"] == nothing_near["count"] == 0
    assert emptied["filtered_out"] > 0 and nothing_near["filtered_out"] == 0, (
        "the two states are indistinguishable, which is the defect this field "
        "exists to close")


def test_filtered_out_is_counted_post_wall_so_it_cannot_report_the_wall_s_removals(vec):
    """**THIS IS WHY THE COUNT IS TAKEN IN TWO STAGES RATHER THAN ONE.** Round 3
    of PR #2 folded the raw candidate count into `truncated` and round 4's
    security review removed it: a pre-filter count is identical for every caller
    whatever its scope, so returning it lets a restricted caller infer that near
    matches exist beyond its boundary.

    `filtered_out` is answerable only because it is counted AFTER the scope clause
    and the customer wall and BEFORE the caller's own `source`/`product`. The
    window here holds five rows; this caller may see three of them. A count taken
    on the wrong side of the wall would report 4 and disclose the two it may not
    see."""
    _index_sourced_signals()
    body = vec.get("/signals/search?q=anything&limit=20&source=ci",
                   headers=_h()).json()
    assert body["count"] == 1
    assert body["filtered_out"] == 2, (
        "`filtered_out` counts rows removed by the scope wall or the customer "
        "wall, which is the pre-filter leak the two-stage count exists to avoid")
    wide = vec.get("/signals/search?q=anything&limit=20&source=ci",
                   headers=_h(WIDE)).json()
    assert wide["filtered_out"] > body["filtered_out"], (
        "a wider caller sees no more rows than a scoped one, so the assertion "
        "above would hold with the wall counted in")


def test_filtered_out_is_zero_when_the_caller_passed_no_filter_of_its_own(vec, monkeypatch):
    """The field is about the caller's OWN narrowing, so with none passed there is
    nothing for it to report — and the second query is not run at all.

    **THE QUERY COUNT IS ASSERTED RATHER THAN THE VALUE, because the value proves
    nothing here** (reviewer, PR #64). With no own filter `scoped_where` and
    `where` are the same clause, so `filtered_out` is 0 whether the skip works,
    is inverted, or is deleted outright — the one new test the two-stage
    mutation cannot reach, since the mutated line sits inside the branch this
    request never enters. Counting the statements is what tells those apart.

    The second half is the control: without it, a build that never ran the second
    query at all would pass the first."""
    _index_sourced_signals()
    statements: list[str] = []
    real_connect = vectors.connect

    class _Counting:
        """Records SQL and delegates everything else, so `vectors.nearest` and
        the row fetch still run against the real connection."""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args, **kwargs):
            statements.append(sql)
            return self._conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(
        vectors, "connect",
        lambda *args, **kwargs: _Counting(real_connect(*args, **kwargs)))

    # Matched on the SIGNALS count specifically: `vectors.available` probes the
    # index with its own `COUNT(*)` on every request, so a bare "COUNT(*)"
    # predicate counts the health check and never reaches zero.
    def boundary_counts():
        return [q for q in statements if "COUNT(*) FROM signals" in q]

    body = vec.get("/signals/search?q=anything&limit=20", headers=_h()).json()
    assert body["count"] == 3 and body["filtered_out"] == 0
    assert boundary_counts() == [], (
        "the boundary-only COUNT ran for a caller that passed no filter of "
        "its own, so the skip is not doing what its comment claims")

    statements.clear()
    filtered = vec.get("/signals/search?q=anything&limit=20&source=ci",
                       headers=_h()).json()
    assert filtered["filtered_out"] == 2
    assert len(boundary_counts()) == 1, (
        "the second query never runs at all, so the assertion above holds "
        "against a build with no two-stage count in it")


def test_an_unbounded_offset_is_refused_rather_than_clamped(client):
    """Clamping a limit gives fewer rows and says so. Clamping an offset would
    return a different page than the one asked for, silently — and on the semantic
    route it also drives the size of the KNN scan."""
    r = client.get(f"/items?offset={libapp.MAX_OFFSET + 1}", headers=_h())
    assert r.status_code == 400
    assert r.json()["max_offset"] == libapp.MAX_OFFSET
    ok = client.get(f"/items?offset={libapp.MAX_OFFSET}", headers=_h())
    assert ok.status_code == 200


# --- the record is as wide as Orient's ---------------------------------------------
# The first shadow run against live data agreed on every row and every shared
# value, and still reported the Library returning a narrower record. These pin the
# thirteen fields that finding named: the eleven now served, and the two omitted on
# purpose. A field quietly dropped again would pass every other test in this file.


def test_an_item_carries_every_field_orient_serves(client):
    body = client.get("/items/issue_v1", headers=_h()).json()
    for field in ("created_by", "resolved_by", "run_id", "parked_at",
                  "claim_expires_at", "claim_expired"):
        assert field in body, field


def test_a_listed_item_carries_them_too(client):
    """The list and the single item must not disagree about the shape. A client
    that paginates and then fetches one row should see the same record twice.

    The two sub-resources are the enumerated exceptions, and enumerating them is
    the point — a divergence nobody listed is a divergence nobody noticed.
    `comments`, `events` and `events_elided` are on the detail only, because a
    200-row page would otherwise carry every thread and every transition log in
    the queue.
    """
    listed = client.get("/items?limit=50", headers=_h(WIDE)).json()["items"]
    single = client.get("/items/issue_v1", headers=_h()).json()
    row = next(i for i in listed if i["id"] == "issue_v1")
    assert set(single) - set(row) == {"comments", "events", "events_elided"}
    assert set(row) - set(single) == set()


def test_the_thread_is_on_the_single_item_and_not_on_the_list(client):
    """A deliberate divergence from Orient, which inlines every comment on every
    row: the API caps a thread at 200 comments of up to 100,000 characters, so a
    200-row page would carry every thread in the queue."""
    listed = client.get("/items?limit=50", headers=_h(WIDE)).json()["items"]
    assert all("comments" not in i for i in listed)
    assert "comments" in client.get("/items/issue_v1", headers=_h()).json()


def test_a_course_carries_its_own_count_and_its_authors(client):
    """`count` on the object means how many epics this Course holds; `count` in
    the envelope means how many Courses are on this page. Same key, two subjects,
    and Orient answers the first — so a client reading it gets the same number
    from both services."""
    body = client.get("/courses", headers=_h(WIDE)).json()
    course = body["courses"][0]
    assert course["count"] == len(course["entries"])
    assert "created_by" in course and "updated_by" in course


def test_a_signal_carries_who_captured_it(client):
    body = client.get("/signals?limit=5", headers=_h(WIDE)).json()
    assert "ingested_by" in body["signals"][0]


def test_the_two_deliberate_omissions_stay_omitted(client):
    """`dedup_key` is an idempotency key a reader could use to forge a collision
    with somebody else's pending write; `embedded_at` is an internal index stamp
    a caller cannot act on. Everything else Orient serves is served."""
    signal = client.get("/signals?limit=5", headers=_h(WIDE)).json()["signals"][0]
    assert "dedup_key" not in signal
    assert "embedded_at" not in signal
    item = client.get("/items/issue_v1", headers=_h()).json()
    assert "once_key" not in item and "embedded_at" not in item


# --- who the caller is ---------------------------------------------------------
#
# `GET /whoami` exists so a client can assert on the grant it was actually given.
# Orient stopped filtering `privacy_class` itself when ADR-0022 gave the privacy
# wall to this service, which means a token misprovisioned as `kind: system` makes
# a full-tier page render customer-class signals with nothing anywhere raising —
# and the key that decides it lives in a gitignored file no review sees. These
# tests hold the two halves that make the route worth having: it tells the truth
# about the caller, and it tells it about the CALLER.


def test_whoami_reports_the_callers_own_resolved_identity(client):
    body = client.get("/whoami", headers=_h()).json()
    assert body == {"actor": "vivy-agent", "kind": "agent",
                    "products": ["vivy"],
                    "capabilities": sorted(identity.ROLE_CAPS["agent"])}


def test_whoami_reports_the_capabilities_the_role_table_grants(client):
    """DERIVED FROM `ROLE_CAPS`, never a list copied into this file. A hand-written
    expectation passes on the day someone edits the role table and the route stops
    agreeing with it, which is the one disagreement this route cannot survive — a
    client asserting on a stale answer is worse off than one that never asked."""
    for token, kind in ((AGENT, "agent"), (SYSTEM, "system")):
        body = client.get("/whoami", headers=_h(token)).json()
        assert body["kind"] == kind
        assert body["capabilities"] == sorted(identity.ROLE_CAPS[kind])


def test_whoami_is_what_orient_can_refuse_on(client):
    """The assertion Orient makes once per process, exercised here as Orient makes
    it. An agent credential must not carry `signal.customer`; the same route must
    show that a system one does, or the check is vacuous and passes against a route
    that returns an empty list."""
    agent = client.get("/whoami", headers=_h(AGENT)).json()
    assert "signal.customer" not in agent["capabilities"]
    system = client.get("/whoami", headers=_h(SYSTEM)).json()
    assert "signal.customer" in system["capabilities"]


def test_whoami_does_not_tell_a_stranger_what_a_token_would_grant(client):
    """401 rather than a description of the tiers. An unauthenticated caller
    learning the capability sets learns which role is worth stealing a token for."""
    r = client.get("/whoami", headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401
    assert "capabilities" not in r.json()


def test_whoami_refuses_a_parameter_rather_than_answering_about_the_caller(client):
    """**Refused, not ignored.** A client author writing `?actor=orient` to inspect
    another identity would otherwise get 200 carrying their OWN capabilities and
    conclude they had checked Orient's — a wrong answer with a success code, which
    is the exact shape this route was built to remove."""
    r = client.get("/whoami?actor=orient", headers=_h())
    assert r.status_code == 400
    assert r.json()["got"] == ["actor"]
    assert "capabilities" not in r.json()


def test_whoami_reports_an_approved_client_through_the_client_token_file(
        client, tmp_path, monkeypatch):
    """The tier ADR-0038 defines resolves through its own file, so a route reading
    `ident.kind` from the wrong one reports `agent` for a credential that holds
    `policy.write`. That is the misreport with the widest blast radius here."""
    client_toks = tmp_path / "client-tokens.json"
    client_toks.write_text(json.dumps({
        "orient-token": {"name": "orient", "products": ["*"]}}))
    monkeypatch.setattr(identity, "CLIENT_TOKENS", client_toks)
    body = client.get("/whoami", headers=_h("orient-token")).json()
    assert body["kind"] == "approved_client"
    assert body["actor"] == "orient"
    assert {"signal.ack", "policy.write"} <= set(body["capabilities"])


def test_whoami_reports_an_empty_scope_as_an_empty_scope(client):
    """A token registered with no products authenticates and reads nothing, which
    `Identity` documents as a real state rather than a bug. The route must say so
    plainly — an operator debugging "why does every list come back empty" needs to
    see the empty scope, not infer it."""
    body = client.get("/whoami", headers=_h(NOSCOPE)).json()
    assert body["products"] == []
    assert body["capabilities"], "a scopeless token still holds its role's caps"


def test_whoami_reports_every_product_in_a_multi_product_scope(client):
    """Every other fixture token carries zero or one product, which left the
    multi-product path asserted by nothing (reviewer, PR #29)."""
    body = client.get("/whoami", headers=_h(MULTI)).json()
    assert set(body["products"]) == {"vivy", "constance", "hollis"}


def test_whoami_sorts_the_scope_it_reports(client, monkeypatch):
    """`products` is a frozenset, so an unsorted response would be right or wrong
    by luck — a client diffing two calls could see its scope "change" with nothing
    having changed.

    **The ordering is asserted against a FIXED order, not against a set's own.**
    Reading a real token's frozenset back and checking it came out sorted is the
    obvious version of this test and it is a flaky one: `PYTHONHASHSEED` is
    randomised per process, so the unsorted answer is already in sorted order on
    some fraction of runs. Planting `list(ident.products)` in place of `sorted(...)`
    caught it on two runs out of three, which is the shape of a test that would
    later be deleted as flaky rather than believed. Handing the route a known
    order makes the plant fail every time.
    """
    monkeypatch.setattr(
        identity, "resolve",
        lambda auth: identity.Identity(actor="fixed", kind="agent",
                                       products=("vivy", "constance", "hollis")))
    body = client.get("/whoami", headers=_h()).json()
    assert body["products"] == ["constance", "hollis", "vivy"]


# --- the transition logs, which were written and never read back ----------------

def _event(table, **cols):
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute(f"INSERT INTO {table} ({','.join(cols)}) "
                 f"VALUES ({','.join('?' * len(cols))})", list(cols.values()))
    conn.commit()
    conn.close()


def test_an_item_carries_its_transition_log(client):
    """**The third write-only log, served because the first two were found the
    expensive way.** `issue_events` is written on every open, claim, resolve,
    park, escalate and link-pr, and was read back by nothing."""
    _event("issue_events", item_id="issue_v1", actor="ops-mcp", from_state=None,
           to_state="open", at="2026-08-01 12:00:00.000000")
    _event("issue_events", item_id="issue_v1", actor="co-vivy", from_state="open",
           to_state="fixed", note="shipped in #12", at="2026-08-02 12:00:00.000000")
    events = client.get("/items/issue_v1", headers=_h()).json()["events"]
    assert [(e["from_state"], e["to_state"]) for e in events] == [
        (None, "open"), ("open", "fixed")]
    assert events[1]["note"] == "shipped in #12", (
        "the note is what the transition meant — a resolve carries its outcome "
        "note, and for wontfix that is the whole content of the decision")
    assert events[0]["at"].endswith("Z")


def test_an_item_with_no_transitions_carries_an_empty_log(client):
    """An empty list rather than a missing key. A client cannot tell "no history"
    from "this route serves no history", which is exactly how three of these
    stayed hidden."""
    assert client.get("/items/issue_v2", headers=_h()).json()["events"] == []


def test_a_course_carries_its_change_log(client):
    """**The note is the point.** A reorder without its reasoning records that
    something moved; the note is why, and it is the only place that survives."""
    _event("course_events", course_id="course_v", actor="ops-mcp",
           action="created", entries='["issue_v2"]', at="2026-08-01 00:00:00")
    _event("course_events", course_id="course_v", actor="ops-mcp",
           action="reordered", entries='["issue_v2"]',
           note="the model lane slots after the Course machinery",
           at="2026-08-02 00:00:00")
    events = client.get("/courses/course_v", headers=_h()).json()["events"]
    assert [e["action"] for e in events] == ["created", "reordered"]
    assert events[1]["note"] == "the model lane slots after the Course machinery"


def test_a_course_event_carries_the_order_it_produced(client):
    """A reorder's subject IS the order it produced — an event carrying only an
    action tells a reader the plan changed and not what it changed to."""
    _event("course_events", course_id="course_v", actor="ops-mcp",
           action="reordered", entries='["issue_v2", "issue_v1"]',
           at="2026-08-03 00:00:00")
    events = client.get("/courses/course_v", headers=_h()).json()["events"]
    assert events[-1]["entries"] == ["issue_v2", "issue_v1"], (
        "entries is parsed rather than served as a JSON string, matching every "
        "other JSON column on this surface")


def test_a_course_event_with_an_unreadable_entries_blob_does_not_fail_the_page(client):
    """`entries` is JSON by convention, and `store._json_or` exists because a
    blob that will not parse is real data."""
    _event("course_events", course_id="course_v", actor="ops-mcp",
           action="reordered", entries="not json", at="2026-08-04 00:00:00")
    r = client.get("/courses/course_v", headers=_h())
    assert r.status_code == 200
    assert r.json()["events"][-1]["entries"] == []


def test_a_long_transition_log_is_bounded_and_says_so(client):
    """**A transition log is bounded at READ time, and that is the difference
    from `comments`** (security-reviewer, convivy/library#36). A comment can be
    refused — the write path caps a thread and the caller does not get to add the
    201st. A transition cannot: it already happened, and a log that declined to
    record it would be an audit trail that lies. So the bound is on the read, and
    the count of what it left out travels with it.
    """
    for i in range(libapp.MAX_EVENTS_SERVED + 12):
        _event("issue_events", item_id="issue_v1", actor="ops-mcp",
               from_state="open", to_state="claimed",
               at=f"2026-08-01 12:{i // 60:02d}:{i % 60:02d}.000000")
    body = client.get("/items/issue_v1", headers=_h()).json()
    assert len(body["events"]) == libapp.MAX_EVENTS_SERVED
    assert body["events_elided"] == 12


def test_the_window_keeps_the_most_recent_transitions(client):
    """The question a history answers is how the thing got to where it is now,
    which is answered from the recent end — and it is handed back oldest-first
    so a reader walks the story forwards even when the window starts mid-way."""
    for i in range(libapp.MAX_EVENTS_SERVED + 5):
        _event("issue_events", item_id="issue_v1", actor=f"actor-{i:03d}",
               from_state="open", to_state="claimed",
               at=f"2026-08-01 12:{i // 60:02d}:{i % 60:02d}.000000")
    events = client.get("/items/issue_v1", headers=_h()).json()["events"]
    assert events[0]["actor"] == "actor-005", "the window did not start after the elided rows"
    assert events[-1]["actor"] == f"actor-{libapp.MAX_EVENTS_SERVED + 4:03d}"


def test_a_course_change_log_is_bounded_too(client):
    """A course event carries a full copy of the plan — up to 500 ids — and a
    reorder is gated only by an optimistic-concurrency version a caller can
    always supply correctly, so a Course reordered weekly for a year would inline
    fifty snapshots into one GET."""
    for i in range(libapp.MAX_EVENTS_SERVED + 3):
        _event("course_events", course_id="course_v", actor="ops-mcp",
               action="reordered", entries='["issue_v2"]',
               at=f"2026-08-01 12:{i // 60:02d}:{i % 60:02d}.000000")
    body = client.get("/courses/course_v", headers=_h()).json()
    assert len(body["events"]) == libapp.MAX_EVENTS_SERVED
    assert body["events_elided"] == 3


def test_a_short_log_elides_nothing(client):
    """Zero rather than absent, for the reason the empty list is a list: a client
    that has to tell "nothing elided" from "this server does not report elision"
    will get it wrong."""
    _event("issue_events", item_id="issue_v1", actor="a", from_state=None,
           to_state="open", at="2026-08-01 12:00:00.000000")
    body = client.get("/items/issue_v1", headers=_h()).json()
    assert body["events_elided"] == 0


def test_search_accepts_the_status_all_sentinel(kb, client):
    """The accepting half of the parity claim `test_reference_reads.py` makes —
    it asserts the refusal, which happens before the query, and this asserts
    acceptance, which needs the `docs_fts` only this file's index builds. Split
    across two files rather than dropped, because a parity claim tested on one
    side only is exactly the drift `_status_clause` exists to prevent
    (issue_01M1MWRXAZ5NRH10NP6Z9MBH0C).

    The archived document is the evidence: it is outside the default corpus, so
    its presence proves the sentinel widened the search rather than merely
    being accepted.
    """
    r = client.get("/reference/search",
                   params={"q": "library", "status": reference.STATUS_ALL},
                   headers=_h())
    assert r.status_code == 200
    assert "platform/decisions/0009-retired" in [
        d["id"] for d in r.json()["documents"]]


@pytest.mark.parametrize("path", [
    "/signals", "/signals/facets", "/signals/pr-state", "/signals/ci-capture"])
def test_a_slow_signals_read_runs_off_the_event_loop(client, monkeypatch, path):
    """Each of these reads scans the whole signal set, 60 to 400ms on the
    live vault on 2026-10-12, and on the event loop one in-flight call raised
    the median of twenty concurrent `/health` calls from 28ms to 494ms (CQ·76,
    issue_01M10P3QVQEVDZAJGTNHMM8KKM). So each must open its connection inside
    `run_in_threadpool`, where no event loop is running.

    Asserted on the thread rather than by timing a concurrent request, which
    would pass or fail with the machine's load."""
    opened_on_loop = []
    real_connect = store.connect

    def recording_connect(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            opened_on_loop.append(True)
        except RuntimeError:
            opened_on_loop.append(False)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(store, "connect", recording_connect)
    r = client.get(path, headers=_h(SYSTEM))
    assert r.status_code == 200, r.text
    assert opened_on_loop, "the route opened no connection through store.connect"
    assert not any(opened_on_loop), f"{path} opened a connection on the event loop"
