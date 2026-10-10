"""The Reference Room's three missing reads: enumerate documents, ask whether one
is checked out, and keep a checkout alive.

`issue_01M0JG8ZRNNW4W1DMKE3SF012V` (status), `issue_01M0JG924BJ3EJDHWEXBM73A4X`
(heartbeat), and the listing route Orient needs to stop opening the knowledge
index file itself.

**The routing assertion is the load-bearing one.** A KB document id contains
slashes, so `GET /reference/documents/{doc_id:path}` matches `.../checkout` as
part of an id. The status route only works because it is registered first, and
a test that only asserted "200 with a checkout key" would pass against the
catch-all returning a document. `test_the_status_route_is_not_shadowed_by_the_
document_route` is what holds the ordering.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import lease
import protocol
import reference
import reference_admission
import store

AGENT = "agent-token-vivy"
DOC_ID = "platform/decisions/0022-library-repo"

# Ids, kinds, projects, statuses and audiences chosen so every filter has both a
# match and a non-match — a filter test whose corpus only contains matches
# passes when the WHERE clause is dropped entirely.
DOCS = [
    (DOC_ID, "decision", "platform", "current", ["team"]),
    ("platform/decisions/0048-deny-list", "decision", "platform", "current",
     ["team", "architect"]),
    ("deck/specs/session-pane", "spec", "deck", "current", ["team"]),
    ("deck/decisions/0002-typed-wire", "decision", "deck", "draft", ["architect"]),
    # `archived` rather than a made-up status: both writers — the knowledge
    # repo's `scripts/validate.py` and agent-platform's `knowledge.py` —
    # reject any status outside {draft, current, deprecated, archived}, so a
    # document carrying one cannot be indexed and a test asserting it is
    # reachable asserts nothing about the real corpus
    # (issue_01M1MWRXAZ5NRH10NP6Z9MBH0C).
    ("vivy/runbooks/archived-thing", "runbook", "vivy", "archived", ["team"]),
]

# **AUDIENCE VALUES THIS SERVICE DOES NOT CONTROL.** The knowledge index is
# built by another repository, so the column can hold anything it writes.
# `json_each` raises `malformed JSON` on a non-NULL, non-parseable value, and
# one such row would 500 the audience filter for every caller (reviewer, PR
# #47). These are inserted as raw column values rather than through the JSON
# encoding above.
RAW_AUDIENCE_DOCS = [
    ("platform/specs/null-audience", "spec", "platform", "current", None, "[]"),
    ("platform/specs/empty-audience", "spec", "platform", "current", "", "[]"),
    ("platform/specs/broken-audience", "spec", "platform", "current",
     "not-json", "[]"),
    # ...and the same for `topics`, which `GET /reference/topics` joins through
    # `json_each` the same way. Pre-existing, fixed alongside.
    ("platform/specs/broken-topics", "spec", "platform", "current",
     '["team"]', "not-json"),
    # A GOOD ROW BESIDE THE BAD ONE, so the topics test proves the route still
    # ANSWERS rather than merely failing to crash — a guard that dropped every
    # row would pass a bare 200 assertion.
    ("platform/specs/good-topics", "spec", "platform", "current",
     '["team"]', '["governance"]'),
    # VALID JSON, WRONG SHAPE. `json_valid` passes all three; `json_each` on an
    # object iterates its VALUES, so an object-shaped audience would match an
    # audience filter naming one of those values (security-reviewer, PR #47
    # round 2). A guard checking only parseability lets that through as a wrong
    # ANSWER rather than a crash, which nothing reports.
    ("platform/specs/object-audience", "spec", "platform", "current",
     '{"a": "architect"}', '{"b": "governance"}'),
    ("platform/specs/scalar-audience", "spec", "platform", "current",
     '"architect"', '123'),
]


def _hash_bodies(iconn):
    """`body_hash` for every fixture row, as `sha256(body)`.

    **Derived from the body rather than written beside it**, matching
    `lib/knowledge/indexer.py`, which hashes the body it is about to store. A
    fixture that carried a hand-written hash could assert a value that was
    never a digest of anything, and the test asserting `body_sha256` would
    pass against a constant.
    """
    for doc_id, body in iconn.execute("SELECT id, body FROM docs").fetchall():
        iconn.execute("UPDATE docs SET body_hash = ? WHERE id = ?",
                      (hashlib.sha256((body or "").encode()).hexdigest(), doc_id))


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE reference_checkouts (
            id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, holder TEXT NOT NULL,
            run_id TEXT, taken_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL,
            released_at TEXT, release_reason TEXT, released_by TEXT);
        CREATE UNIQUE INDEX ix_reference_checkouts_live_doc
            ON reference_checkouts (doc_id) WHERE released_at IS NULL;
    """)
    conn.commit()
    conn.close()

    index = tmp_path / "index.db"
    iconn = sqlite3.connect(index)
    iconn.executescript("""
        CREATE TABLE docs (
            id TEXT PRIMARY KEY, source_ref TEXT, kind TEXT, project TEXT,
            title TEXT, status TEXT, topics TEXT, audience TEXT,
            last_reviewed TEXT, updated_at TEXT, body TEXT, body_hash TEXT);
    """)
    for doc_id, kind, project, status, audience in DOCS:
        iconn.execute(
            "INSERT INTO docs (id, source_ref, kind, project, title, status, "
            "topics, audience, body, last_reviewed, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, f"{doc_id}.md", kind, project, f"title of {doc_id}",
             status, "[]", json.dumps(audience), f"body of {doc_id}",
             "2026-08-01", "2026-08-02"))
    for doc_id, kind, project, status, raw_audience, raw_topics in RAW_AUDIENCE_DOCS:
        iconn.execute(
            "INSERT INTO docs (id, source_ref, kind, project, title, status, "
            "topics, audience, body, last_reviewed, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, f"{doc_id}.md", kind, project, f"title of {doc_id}",
             status, raw_topics, raw_audience, f"body of {doc_id}",
             "2026-08-01", "2026-08-02"))
    _hash_bodies(iconn)
    iconn.commit()
    iconn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
    }))

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(reference, "INDEX_DB", index)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return TestClient(libapp.app)


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _list(client, **params):
    return client.get("/reference/documents", params=params, headers=_h())


def _ids(r):
    return [d["id"] for d in r.json()["documents"]]


def _status(client, doc_id=DOC_ID):
    return client.get(f"/reference/documents/{doc_id}/checkout", headers=_h())


def _checkout(client, doc_id=DOC_ID, **body):
    return client.post(f"/reference/documents/{doc_id}/checkout", json=body,
                       headers=_h())


def _beat(client, doc_id=DOC_ID, **body):
    return client.post(f"/reference/documents/{doc_id}/heartbeat", json=body,
                       headers=_h())


def _age_checkout(doc_id, *, minutes):
    """Push a live checkout's timestamps back so it reads as stale or dead."""
    when = (_dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
            - _dt.timedelta(minutes=minutes)).isoformat(" ")
    conn = sqlite3.connect(store.DB_PATH)
    try:
        conn.execute("UPDATE reference_checkouts SET taken_at = ?, "
                     "heartbeat_at = ? WHERE doc_id = ? AND released_at IS NULL",
                     (when, when, doc_id))
        conn.commit()
    finally:
        conn.close()


def _row(doc_id=DOC_ID):
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM reference_checkouts WHERE doc_id = ? "
            "AND released_at IS NULL", (doc_id,)).fetchone()
    finally:
        conn.close()


# --- the index cannot be opened ------------------------------------------------
#
# `issue_01M1N7J265ST2V6VZ2C9TA4BN7`: all six Reference Room routes shared one
# unguarded `reference.connect()` call, and a missing index turned into an
# unexplained 500 rather than a refusal — twelve occurrences in `serve.log` on
# 2026-09-03, all through `list_reference_documents`, none of them caught.


REFERENCE_ROUTES = [
    ("get", "/reference/search", {"q": "anything"}),
    ("get", "/reference/topics", {}),
    ("get", "/reference/documents", {}),
    ("get", f"/reference/documents/{DOC_ID}/checkout", {}),
    ("get", f"/reference/documents/{DOC_ID}", {}),
    ("post", f"/reference/documents/{DOC_ID}/checkout", {}),
]


@pytest.mark.parametrize("method, path, params", REFERENCE_ROUTES)
def test_every_reference_route_refuses_503_when_the_index_is_unreadable(
        client, monkeypatch, tmp_path, method, path, params):
    """**503, not 404**: the index is expected to exist, and its absence is the
    service unable to serve rather than a document that is not there. All six
    routes are exercised so a seventh call site skipping the shared exception
    handler would show up here rather than only in the one route with a
    periodic caller loud enough to have surfaced this originally."""
    monkeypatch.setattr(reference, "INDEX_DB", tmp_path / "gone.db")
    if method == "get":
        r = client.get(path, params=params, headers=_h())
    else:
        r = client.post(path, json=params, headers=_h())
    assert r.status_code == 503
    assert "knowledge index" in r.json()["error"]
    # THE HINT NAMES THE CONDITION AND NOT THE PATH. The index's absolute
    # filesystem path — and the service's home-directory layout and OS
    # username with it — reached any caller holding `reference.read` before
    # this; the path now goes to the service log instead.
    assert "gone.db" not in r.json()["hint"]
    assert "LIBRARY_KB_INDEX" in r.json()["hint"]


# --- the listing --------------------------------------------------------------


def test_listing_returns_the_default_status_corpus(client):
    """`archived` is excluded by default, matching search — the two surfaces have
    to agree about what exists."""
    r = _list(client)
    assert r.status_code == 200
    assert "vivy/runbooks/archived-thing" not in _ids(r)
    assert r.json()["total"] == 11


def test_listing_is_ordered_by_id_so_paging_is_stable(client):
    assert _ids(_list(client)) == sorted(_ids(_list(client)))
    # PAGED TO COMPLETION rather than two pages compared against the whole, so
    # the assertion holds however the corpus grows. NO OVERLAP AND NO GAP: an
    # unordered listing can return one row twice and skip another, and a caller
    # walking to the end never sees it happen.
    walked, offset = [], 0
    while True:
        page = _list(client, limit=2, offset=offset)
        walked += _ids(page)
        if not page.json()["truncated"]:
            break
        offset += page.json()["count"]
    assert len(walked) == len(set(walked))
    assert walked == _ids(_list(client, limit=100))


def test_listing_omits_bodies(client):
    assert all("body" not in d for d in _list(client).json()["documents"])


# A body shaped like the real corpus: a heading, a list, an HTML comment, an
# emphasised prose lead, and a trailing provenance section. **The provenance
# section is the case that proves which body was measured**: Orient's
# `_clean_body` strips it, so a cleaned length would differ from this one.
PROVENANCE_BODY = (
    "# The heading\n"
    "\n"
    "<!-- unsupported notion block:\n"
    "file -->\n"
    "- a list item\n"
    "**Bold lead** with a [link](https://example.com/x) and `code`.\n"
    "\n"
    "## Provenance\n"
    "\n"
    "Drafted by the Library crew on 2026-09-11.\n")


def _set_body(doc_id, body):
    """Change a body the way the indexer would, body and hash together."""
    iconn = sqlite3.connect(reference.INDEX_DB)
    iconn.execute("UPDATE docs SET body = ? WHERE id = ?", (body, doc_id))
    _hash_bodies(iconn)
    iconn.commit()
    iconn.close()


def test_listing_rows_carry_the_stored_bodys_blurb_and_length(client):
    _set_body(DOC_ID, PROVENANCE_BODY)
    row = next(d for d in _list(client).json()["documents"] if d["id"] == DOC_ID)
    assert row["blurb"] == "Bold lead with a link and code."
    assert row["body_len"] == len(PROVENANCE_BODY)
    # The single-document read serves the same text the length measures.
    whole = client.get(f"/reference/documents/{DOC_ID}", headers=_h()).json()
    assert row["body_len"] == len(whole["body"])


def test_every_listing_row_carries_both_fields(client):
    docs = _list(client).json()["documents"]
    assert docs and all(d["blurb"] == f"body of {d['id']}" for d in docs)
    assert all(d["body_len"] == len(f"body of {d['id']}") for d in docs)


def test_body_len_counts_characters_not_bytes(client):
    _set_body(DOC_ID, "café — naïve\n")
    row = next(d for d in _list(client).json()["documents"] if d["id"] == DOC_ID)
    assert row["body_len"] == 13


@pytest.mark.parametrize("body, expected", [
    # every skipped line shape, ahead of the prose line that wins
    (("# h\n---\n| a | b |\n```\n> quoted\n- item\n* item\n+ item\n12. item\n"
      "Prose.\n"), "Prose."),
    # an emphasised lead is prose, not a `* ` bullet
    ("**Bold** lead\n", "Bold lead"),
    ("*Italic* lead\n", "Italic lead"),
    # an HTML comment spanning lines is removed before lines are considered
    ("<!-- a\nb -->\nAfter.\n", "After."),
    # nothing qualifies
    ("# only a heading\n\n- and a list\n", ""),
    ("", ""),
])
def test_blurb_takes_the_first_prose_line(body, expected):
    assert reference.blurb(body) == expected


def test_blurb_is_cut_to_its_bound():
    assert reference.blurb("x" * 500) == "x" * reference.BLURB_MAX


@pytest.mark.parametrize("body", [
    "a <!-- one --> b <!-- two --> c",
    "a <!-- spans\nlines --> b",
    "a <!-- unclosed b",
    "a <!-- closed --> b <!-- unclosed c",
    "a <!-- <!-- nested --> b -->",
    "a <!--> b --> c",
    "a <!---> b",
    "-->  <!-- x -->",
    "",
])
def test_the_comment_strip_is_the_regex_it_replaces(body):
    """Orient's rule is `re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)`.
    The linear strip must give its result exactly, including the overlapping
    `<!-->` shapes where a naive `find("-->", start)` would close early."""
    import re
    expected = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    assert reference._strip_html_comments(body) == expected


@pytest.mark.parametrize("body", [
    "<!--" * 250_000,
    "[" * 1_000_000,
    "[a](" * 250_000,
])
def test_blurb_stays_linear_on_unclosed_markup(body):
    """Each of these took seconds at tens of thousands of characters and grew
    at least quadratically before the fix (security-reviewer, PR #107). A
    million characters must finish well inside the bound, which is generous so
    a slow CI runner does not flake it; the old code would take hours."""
    import time
    started = time.monotonic()
    reference.blurb(body)
    assert time.monotonic() - started < 2.0


def test_a_first_line_past_the_scan_bound_still_gives_a_blurb():
    line = "[link](u) " + "y" * (reference.BLURB_SCAN * 2)
    assert reference.blurb(line) == ("link " + "y" * 300)[:reference.BLURB_MAX]


def test_listing_reports_truncation_and_total(client):
    r = _list(client, limit=2)
    assert r.json()["count"] == 2 and r.json()["total"] == 11
    assert r.json()["truncated"] is True
    assert _list(client).json()["truncated"] is False


def test_the_fleet_wide_view_is_the_unfiltered_one(client):
    """The operator, 2026-08-22: one route serves both. Omitting `project` is Orient's
    view."""
    assert set(_ids(_list(client))) == {
        DOC_ID, "platform/decisions/0048-deny-list", "deck/specs/session-pane",
        "deck/decisions/0002-typed-wire", "platform/specs/null-audience",
        "platform/specs/empty-audience", "platform/specs/broken-audience",
        "platform/specs/broken-topics", "platform/specs/good-topics",
        "platform/specs/object-audience", "platform/specs/scalar-audience"}


def test_the_crew_scoped_view_is_the_same_route_with_project(client):
    """...and passing one is Deck's view."""
    assert set(_ids(_list(client, project="deck"))) == {
        "deck/specs/session-pane", "deck/decisions/0002-typed-wire"}


def test_kind_filters(client):
    assert "deck/specs/session-pane" in _ids(_list(client, kind="spec"))
    assert "platform/decisions/0048-deny-list" not in _ids(_list(client, kind="spec"))


def test_status_filters_and_can_reach_the_excluded_ones(client):
    assert _ids(_list(client, status="archived")) == [
        "vivy/runbooks/archived-thing"]


def test_audience_matches_membership_not_equality(client):
    """`audience` is a JSON array. `?audience=architect` has to match a document
    listing it alongside others, which a naive `=` against the stored text
    would miss."""
    got = set(_ids(_list(client, audience="architect")))
    assert got == {"platform/decisions/0048-deny-list",
                   "deck/decisions/0002-typed-wire"}


def test_audience_does_not_match_a_substring_of_another_audience(client):
    """A `LIKE '%team%'` implementation would pass every test above and fail
    this one."""
    assert _ids(_list(client, audience="tea")) == []


def test_filters_combine(client):
    assert _ids(_list(client, project="deck", status="draft")) == [
        "deck/decisions/0002-typed-wire"]


def test_listing_needs_reference_read(client, monkeypatch):
    monkeypatch.setattr(identity, "authorized", lambda ident, cap: False)
    r = _list(client)
    assert r.status_code == 403 and "reference.read" in r.json()["error"]


# --- the checkout status read -------------------------------------------------


def test_the_status_route_is_not_shadowed_by_the_document_route(client):
    """**THE ORDERING TEST.** A doc id contains slashes, so the catch-all
    `GET /reference/documents/{doc_id:path}` matches `.../checkout` as part of an
    id. Registered after it, this route is unreachable and the catch-all answers
    with a 404 on a document id that ends in `/checkout`. Asserting the
    `checkout` key is what distinguishes the two."""
    r = _status(client)
    assert r.status_code == 200
    body = r.json()
    assert "checkout" in body and body["doc_id"] == DOC_ID
    # ...and it is NOT the document route's answer.
    assert "source_ref" not in body


def test_a_free_document_reports_a_null_checkout(client):
    assert _status(client).json()["checkout"] is None


def test_a_held_document_names_its_holder(client):
    _checkout(client)
    body = _status(client).json()["checkout"]
    assert body["holder"] == "vivy-agent" and body["released_at"] is None


def test_the_status_read_takes_nothing(client):
    """The whole point of the route: asking must not lock."""
    _status(client)
    assert _row() is None
    # ...and a subsequent checkout by anyone still succeeds.
    assert _checkout(client).status_code == 200


def test_a_dead_checkout_is_reported_rather_than_hidden(client):
    """`held by a session that went away` and `free` are different facts to a
    caller deciding whether to take it, and `take` treats them differently too."""
    _checkout(client)
    _age_checkout(DOC_ID, minutes=int(lease.max_lease().total_seconds() / 60) + 120)
    body = _status(client).json()["checkout"]
    assert body is not None and body["holder"] == "vivy-agent"


def test_status_404s_on_a_document_that_is_not_indexed(client):
    """Answering `checkout: null` would report `free` for something nobody can
    ever take."""
    assert _status(client, doc_id="platform/decisions/9999-invented").status_code == 404


def test_status_needs_reference_read(client, monkeypatch):
    monkeypatch.setattr(identity, "authorized", lambda ident, cap: False)
    assert _status(client).status_code == 403


# --- the heartbeat ------------------------------------------------------------


def test_a_heartbeat_moves_the_expiry(client):
    _checkout(client)
    _age_checkout(DOC_ID, minutes=5)
    before = _row()["heartbeat_at"]
    r = _beat(client)
    assert r.status_code == 200
    assert _row()["heartbeat_at"] > before


def test_a_heartbeat_never_moves_taken_at(client):
    """The hard cap is what bounds a misnamed heartbeat, and it keys on
    `taken_at`. If a heartbeat moved it, a session could hold a document
    forever by beating."""
    _checkout(client)
    taken = _row()["taken_at"]
    _age_checkout(DOC_ID, minutes=5)
    taken_after_aging = _row()["taken_at"]
    _beat(client)
    assert _row()["taken_at"] == taken_after_aging != taken


def test_a_heartbeat_on_nothing_is_a_404_and_creates_no_checkout(client):
    """A heartbeat that could CREATE a checkout would be a checkout by another
    name, and one that skipped the conflict check."""
    r = _beat(client)
    assert r.status_code == 404
    assert _row() is None


def test_a_heartbeat_by_a_different_holder_is_refused(client):
    _checkout(client, owner="someone-else")
    r = _beat(client, owner="not-the-holder")
    assert r.status_code == 404
    # ...and the real holder's checkout is untouched.
    assert _row()["holder"] == "someone-else"


def test_a_dead_checkout_cannot_be_revived_by_a_heartbeat(client):
    """Past its grace the document is already reclaimable; reviving it here
    would reopen the window a reclaim just closed."""
    _checkout(client)
    _age_checkout(DOC_ID, minutes=int(lease.max_lease().total_seconds() / 60) + 120)
    assert _beat(client).status_code == 404


def test_the_heartbeat_refusal_does_not_say_who_holds_it(client):
    """One message for three causes. Distinguishing them would tell a caller who
    holds a document it did not ask about; the status route answers that, and
    answers it the same way to everyone."""
    _checkout(client, owner="someone-else")
    assert "someone-else" not in _beat(client, owner="mine").json()["error"]


def test_heartbeat_404s_on_a_document_that_is_not_indexed(client):
    assert _beat(client, doc_id="platform/decisions/9999-invented").status_code == 404


def test_heartbeat_needs_reference_checkout(client, monkeypatch):
    monkeypatch.setattr(identity, "authorized", lambda ident, cap: False)
    r = _beat(client)
    assert r.status_code == 403 and "reference.checkout" in r.json()["error"]


# --- audience values this service does not control ----------------------------


@pytest.mark.parametrize("value", ["team", "architect", "anything"])
def test_a_malformed_audience_row_does_not_500_the_filter(client, value):
    """**One unparseable row would take the route down for every caller**, not
    just for that document (reviewer, PR #47). `json_each` raises `malformed
    JSON` on a non-NULL, non-JSON value, and the knowledge index is written by
    another repository, so this is data this service cannot guarantee.

    `reference.build_match` already engineers around the same class on the
    search path — "a malformed MATCH is a 500 on a read path" — and `doc_dict`
    reads this very column through `_json_or`. The filter has to hold the same
    line."""
    r = _list(client, audience=value)
    assert r.status_code == 200


def test_rows_with_unusable_audience_match_no_audience_filter(client):
    """NULL, empty and unparseable all mean "no audience this route can read",
    so they match nothing rather than matching everything."""
    for value in ("team", "architect"):
        got = _ids(_list(client, audience=value))
        assert "platform/specs/null-audience" not in got
        assert "platform/specs/empty-audience" not in got
        assert "platform/specs/broken-audience" not in got


def test_a_malformed_audience_row_is_still_listed_unfiltered(client):
    """It is excluded from an AUDIENCE query, not hidden from the Reference
    Room. A document with a bad frontmatter field still exists."""
    assert "platform/specs/broken-audience" in _ids(_list(client))


def test_the_audience_filter_still_works_alongside_the_bad_rows(client):
    """The guard must not have disabled the filter it is protecting."""
    assert set(_ids(_list(client, audience="architect"))) == {
        "platform/decisions/0048-deny-list", "deck/decisions/0002-typed-wire"}


def test_a_malformed_topics_row_does_not_500_the_topics_route(client):
    """**Pre-existing, and the same defect as the audience filter one join
    over** (security-reviewer, PR #47). `GET /reference/topics` joins
    `json_each(d.topics)` unguarded, so one unparseable `topics` value anywhere
    in the visible corpus aborts the statement and takes the route down for
    every caller. Guarding the new filter and leaving this one would not be a
    fix; it would be the same bug with a smaller blast radius."""
    r = client.get("/reference/topics", headers=_h())
    assert r.status_code == 200
    # ...and it still ANSWERS: the good row's topic survives the guard, so this
    # is not a route that returns 200 by dropping everything.
    assert any(t["topic"] == "governance" for t in r.json()["topics"])


def test_a_valid_but_non_array_audience_does_not_match(client):
    """**Valid JSON of the wrong shape is a wrong ANSWER, not a crash**
    (security-reviewer, PR #47 round 2). `json_each('{"a": "architect"}')`
    yields `value = 'architect'`, so a guard checking only `json_valid` would
    match this document on `?audience=architect` when its audience was never
    `["architect"]`. `reference_admission.LIST_FIELDS` already requires these to
    be arrays on the write side; the read side has to agree."""
    got = _ids(_list(client, audience="architect"))
    assert "platform/specs/object-audience" not in got
    assert "platform/specs/scalar-audience" not in got
    # ...and the genuinely-array rows still match.
    assert set(got) == {"platform/decisions/0048-deny-list",
                        "deck/decisions/0002-typed-wire"}


def test_a_non_array_topics_value_is_excluded_from_the_vocabulary(client):
    """Same rule on the topics join: an object's values are not topics."""
    r = client.get("/reference/topics", headers=_h())
    assert r.status_code == 200
    topics = {t["topic"] for t in r.json()["topics"]}
    assert "governance" in topics          # the array-shaped row survives
    assert r.json()["topics"]              # and the route still answers


# --- status: the sentinel, the refusal, and the vocabulary ---------------------


def test_status_all_returns_every_status(client):
    """**The corpus count is not derivable any other way.** `total` is scoped to
    the filter, so a caller wanting every document had to issue one call per
    status and union them — which hardcodes this vocabulary in the caller, and
    a status added later is then dropped silently with no cheaper check
    available to notice (issue_01M1MWRXAZ5NRH10NP6Z9MBH0C)."""
    got = set(_ids(_list(client, status=reference.STATUS_ALL, limit=100)))
    assert got == {d[0] for d in DOCS} | {d[0] for d in RAW_AUDIENCE_DOCS}


def test_status_all_reaches_what_the_default_excludes(client):
    """The sentinel is not merely the default under another name: the document
    the default hides is in it."""
    assert "vivy/runbooks/archived-thing" in _ids(
        _list(client, status=reference.STATUS_ALL))


def test_status_all_reports_the_whole_corpus_as_total(client):
    """`total` follows the filter, which is the reason the sentinel exists —
    under `all` it is the corpus count a caller could not otherwise get."""
    r = _list(client, status=reference.STATUS_ALL)
    assert r.json()["total"] == len(DOCS) + len(RAW_AUDIENCE_DOCS)


@pytest.mark.parametrize("bad", ["retired", "ALL", "current,draft", "*", ""])
def test_an_unrecognised_status_is_refused_rather_than_answered_empty(client, bad):
    """**A silent zero is indistinguishable from a true empty**, which is the
    shape where a broken call and a working one produce identical evidence.
    `?status=all` answered `200 {total: 0}` before this — a reasonable guess,
    and the first thing anyone tries, yielding a plausible empty page with
    nothing saying the parameter was rejected.

    `ALL` is in the list deliberately: the sentinel is case-sensitive, matching
    every other value this filter takes, so a caller shouting it gets told
    rather than silently emptied.

    The empty string is the one case that must NOT refuse — `?status=` is how a
    client serialises an absent optional field, and treating it as a bad value
    would refuse a request that means "no filter". It falls to the default
    instead, which is what `request.query_params.get` returning `""` already
    made it do.
    """
    r = _list(client, status=bad)
    if bad == "":
        assert r.status_code == 200
        return
    assert r.status_code == 400
    assert "status must be one of" in r.json()["error"]
    assert reference.STATUS_ALL in r.json()["error"]


def test_the_refusal_names_the_whole_permitted_set_including_the_sentinel(client):
    """A refusal a caller cannot act on wastes the call twice. Naming `all`
    alongside the four is what turns "your guess is wrong" into "here is the
    spelling you wanted"."""
    error = _list(client, status="nonsense").json()["error"]
    for value in sorted(reference.ALLOWED_STATUSES | {reference.STATUS_ALL}):
        assert value in error


def test_search_refuses_an_unrecognised_status_as_the_listing_does(client):
    """**One helper serves both routes**, because the contract promises they
    answer about the same corpus — a listing that accepted a status search
    refused would make the two disagree about what exists, and the default was
    already shared by copy, which is the arrangement that lets one drift the
    day somebody edits only the other.

    Only the REFUSAL is asserted here, because it happens before the query and
    this fixture builds no `docs_fts`. The accepting half is asserted in
    `test_read_surface.py`, whose index has one — split rather than dropped,
    since a parity claim tested on one side only is the shape this helper
    exists to prevent."""
    assert client.get("/reference/search",
                      params={"q": "library", "status": "nonsense"},
                      headers=_h()).status_code == 400


def test_the_read_and_write_status_vocabularies_agree():
    """**Two copies of one vocabulary, pinned against each other.** The read
    side refuses a status the write side would have accepted only if these
    drift, and a document admitted under such a status becomes unreachable by
    the filter that would find it. Held the way the BM25 constants are held:
    asserted here rather than imported across, so the failure is a test rather
    than an import cycle."""
    assert reference.ALLOWED_STATUSES == reference_admission.ALLOWED_STATUSES
